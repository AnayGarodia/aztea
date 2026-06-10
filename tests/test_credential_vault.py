"""Credential vault tests (Phase 4, E3).

Pins the invariants that make this safe to ship gated: at-rest encryption with no
plaintext leak in any return, fail-closed without a KEK or flag, AAD row-binding,
mandate-scoped decryption, and crypto-shred on revoke.
"""

from __future__ import annotations

import base64
import os

import pytest

import core.db as _db
from core import credential_vault as cv
from core.migrate import apply_migrations

_MANDATE = {"caller_owner_id": "u", "allowed_domains": ["shop.example.com"]}


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    """A vault on an isolated DB with a local KEK + the vault flag enabled."""
    db = str(tmp_path / "vault.db")
    apply_migrations(db)
    monkeypatch.setattr(cv, "DB_PATH", db)
    monkeypatch.setenv("AZTEA_CREDENTIAL_VAULT_ENABLED", "1")
    monkeypatch.setenv("AZTEA_VAULT_ALLOW_LOCAL_KEK", "1")
    monkeypatch.setenv("AZTEA_VAULT_LOCAL_KEK", base64.b64encode(os.urandom(32)).decode())
    return monkeypatch


def _store(**kw):
    base = {"owner_id": "u", "domain": "shop.example.com", "cred_kind": "password",
            "secret": {"username": "alice", "password": "hunter2"}}
    base.update(kw)
    return cv.store_credential(**base)


def test_store_returns_metadata_only(vault):
    meta = _store(domain="Shop.Example.com/login", label="my shop")
    assert "password" not in meta and "hunter2" not in str(meta)  # no secret, no plaintext
    assert meta["domain"] == "shop.example.com" and meta["cred_kind"] == "password"


def test_list_is_metadata_only(vault):
    _store()
    rows = cv.list_credentials("u")
    assert len(rows) == 1 and "hunter2" not in str(rows) and "password" not in rows[0]


def test_disabled_by_default(tmp_path, monkeypatch):
    db = str(tmp_path / "v.db")
    apply_migrations(db)
    monkeypatch.setattr(cv, "DB_PATH", db)
    monkeypatch.delenv("AZTEA_CREDENTIAL_VAULT_ENABLED", raising=False)
    with pytest.raises(cv.VaultUnavailable):
        cv.list_credentials("u")


def test_fail_closed_without_kek(tmp_path, monkeypatch):
    db = str(tmp_path / "v.db")
    apply_migrations(db)
    monkeypatch.setattr(cv, "DB_PATH", db)
    monkeypatch.setenv("AZTEA_CREDENTIAL_VAULT_ENABLED", "1")
    for k in ("AZTEA_VAULT_LOCAL_KEK", "AZTEA_VAULT_ALLOW_LOCAL_KEK", "AZTEA_VAULT_KMS_KEY_ID"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(cv.VaultUnavailable):
        _store()


def test_local_kek_requires_explicit_allow_flag(tmp_path, monkeypatch):
    db = str(tmp_path / "v.db")
    apply_migrations(db)
    monkeypatch.setattr(cv, "DB_PATH", db)
    monkeypatch.setenv("AZTEA_CREDENTIAL_VAULT_ENABLED", "1")
    monkeypatch.setenv("AZTEA_VAULT_LOCAL_KEK", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("AZTEA_VAULT_ALLOW_LOCAL_KEK", raising=False)  # key set, allow flag NOT
    with pytest.raises(cv.VaultUnavailable):
        _store()


def test_injection_round_trips_and_is_gated(vault):
    _store()
    with pytest.raises(cv.VaultUnavailable):  # injection flag off by default
        cv._decrypt_for_injection(owner_id="u", domain="shop.example.com",
                                  cred_kind="password", mandate=_MANDATE)
    vault.setenv("AZTEA_CREDENTIAL_INJECTION_ENABLED", "1")
    cred = cv._decrypt_for_injection(owner_id="u", domain="shop.example.com",
                                     cred_kind="password", mandate=_MANDATE)
    assert cred.username == "alice" and cred.password == "hunter2"
    assert "hunter2" not in repr(cred)  # redacted repr never leaks the secret


def test_injection_refuses_domain_outside_mandate(vault):
    vault.setenv("AZTEA_CREDENTIAL_INJECTION_ENABLED", "1")
    _store(domain="evil.com", secret={"username": "a", "password": "p"})
    with pytest.raises(cv.VaultError):
        cv._decrypt_for_injection(owner_id="u", domain="evil.com",
                                  cred_kind="password", mandate=_MANDATE)


def test_injection_refuses_mismatched_owner(vault):
    vault.setenv("AZTEA_CREDENTIAL_INJECTION_ENABLED", "1")
    _store()
    other = {"caller_owner_id": "someone_else", "allowed_domains": ["shop.example.com"]}
    with pytest.raises(cv.VaultError):
        cv._decrypt_for_injection(owner_id="u", domain="shop.example.com",
                                  cred_kind="password", mandate=other)


def test_aad_swap_fails_to_decrypt(vault):
    _store()
    with _db.get_raw_connection(cv.DB_PATH) as conn:
        row = dict(conn.execute(
            "SELECT * FROM website_credentials WHERE status='active'").fetchone())
    row["owner_id"] = "attacker"  # break the identity the ciphertext is bound to
    with pytest.raises(Exception):  # cryptography.exceptions.InvalidTag
        cv._decrypt_row(row)


def test_revoke_crypto_shreds(vault):
    meta = _store()
    assert cv.revoke_credential(meta["credential_id"], "u") is True
    assert cv.list_credentials("u") == []
    with _db.get_raw_connection(cv.DB_PATH) as conn:
        row = dict(conn.execute(
            "SELECT ciphertext, wrapped_dek, status FROM website_credentials "
            "WHERE credential_id=%s", (meta["credential_id"],)).fetchone())
    assert row["status"] == "revoked"
    assert bytes(row["ciphertext"]) == b"" and bytes(row["wrapped_dek"]) == b""


def test_store_replaces_active_and_rotate_bumps_version(vault):
    _store(secret={"username": "a", "password": "p1"})
    _store(secret={"username": "a", "password": "p2"})  # re-store replaces under the unique scope
    assert len(cv.list_credentials("u")) == 1
    meta = cv.rotate_credential(owner_id="u", domain="shop.example.com",
                                cred_kind="password", new_secret={"username": "a", "password": "p3"})
    assert meta["version"] == 2
    vault.setenv("AZTEA_CREDENTIAL_INJECTION_ENABLED", "1")
    cred = cv._decrypt_for_injection(owner_id="u", domain="shop.example.com",
                                     cred_kind="password", mandate=_MANDATE)
    assert cred.password == "p3"  # latest secret decrypts under the bumped version/AAD


def test_validate_secret_shape(vault):
    with pytest.raises(cv.VaultError):
        _store(secret={"username": "a"})  # missing password
    with pytest.raises(cv.VaultError):
        cv.store_credential(owner_id="u", domain="x.com", cred_kind="totp", secret={})
