"""Encrypted website-credential vault for the write-web (Phase 4, E3). Fail-closed.

# OWNS: at-rest encryption + lifecycle (store/list/revoke/rotate) of a user's
#        website logins so web_actor can act on their existing accounts, plus the
#        in-process decrypt-for-injection used only on the gated write path.
# NOT OWNS: the browser injection itself (agents._web_interact.perform_login), the
#           mandate lifecycle (core.action_mandates), or feature gating
#           (core.feature_flags).
# INVARIANTS:
#   * No plaintext at rest: secrets live ONLY as AES-256-GCM ciphertext; the DEK is
#     wrapped by a KEK that never touches this table.
#   * No silent weak fallback: with no KEK configured, store/decrypt raise
#     VaultUnavailable — the OSS build never persists secrets under a weaker scheme.
#   * Secrets are NEVER returned by store/list/rotate (metadata only) and NEVER logged
#     (Credential.__repr__ is redacted).
#   * AAD binds each ciphertext to {credential_id, owner_id, domain, cred_kind,
#     version}: a row-swap fails to decrypt.
# DECISIONS:
#   * Envelope encryption with a per-credential random DEK; the KEK is hosted KMS
#     (boto3, already a dependency) or an explicitly opted-in local key for self-host.
#   * Crypto-shred on revoke: the ciphertext + wrapped DEK are overwritten with empty
#     bytes so a later DB dump can't recover a revoked secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core import crypto
from core import db as _db
from core import feature_flags

DB_PATH = _db.DB_PATH
_local = _db._local

_AAD_SCHEME = "aztea/cred-aad/1"
_LOCAL_WRAP_AAD = b"aztea/cred-dek-wrap/1"
_SCHEME_KMS = "AESGCM+kms-dek/1"
_SCHEME_LOCAL = "AESGCM+local-dek/1"
_DEK_BITS = 256
_NONCE_BYTES = 12
_LOCAL_KEK_BYTES = 32
_VALID_KINDS = ("password", "totp", "cookies")
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "password": ("username", "password"),
    "totp": ("totp_secret",),
    "cookies": ("cookies",),
}
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
# Crypto-shred placeholder: a revoked row keeps NOT NULL satisfied but holds no secret.
_SHRED = b""


class VaultError(Exception):
    """A vault operation failed for a caller-visible reason (bad input, no match)."""


class VaultUnavailable(VaultError):
    """The vault cannot operate: disabled by flag, or no KEK provider configured.

    Distinct from VaultError so callers can map it to a 'configure me' envelope
    rather than a generic failure.
    """


class Credential:
    """A decrypted secret, in-process only. Never returned over HTTP, never logged.

    scrub() is best-effort (Python cannot guarantee no copies linger), so it is a
    defence-in-depth measure, not a guarantee — call it in a finally after injection.
    """

    def __init__(self, *, kind: str, data: dict[str, Any]):
        self.kind = kind
        self._data = data

    @property
    def username(self) -> str:
        return str(self._data.get("username", ""))

    @property
    def password(self) -> str:
        return str(self._data.get("password", ""))

    @property
    def totp_secret(self) -> str:
        return str(self._data.get("totp_secret", ""))

    @property
    def cookies(self) -> list[dict[str, Any]]:
        raw = self._data.get("cookies")
        return list(raw) if isinstance(raw, list) else []

    def scrub(self) -> None:
        for key in list(self._data.keys()):
            self._data[key] = ""
        self._data.clear()

    def __repr__(self) -> str:  # never leak the secret into a log line or traceback
        return f"<Credential kind={self.kind} (redacted)>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> _db.DbConnection:
    return _db.get_raw_connection(DB_PATH)


def _new_id() -> str:
    n = int.from_bytes(secrets.token_bytes(16), "big")
    chars: list[str] = []
    while n:
        n, rem = divmod(n, 62)
        chars.append(_BASE62[rem])
    return "cred_" + "".join(reversed(chars)).rjust(22, "0")


def normalize_domain(raw: str) -> str:
    """Lowercase host form, scheme + path/query/fragment stripped. Matching against a
    mandate's allowed_domains uses this so 'https://Shop.Example.com/cart',
    'Shop.Example.com/login', and 'shop.example.com' all resolve identically."""
    s = str(raw or "").strip().lower()
    if "://" in s:
        return (urlparse(s).hostname or "").rstrip("/")
    # Bare host or host/path: drop any path, query, or fragment.
    return s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


# --------------------------------------------------------------------------- #
# KEK selection + DEK wrapping (the OSS / hosted boundary)
# --------------------------------------------------------------------------- #


def _kms_key_id() -> str:
    return os.environ.get("AZTEA_VAULT_KMS_KEY_ID", "").strip()


def _local_kek() -> bytes | None:
    """The opted-in local KEK, or None. Requires BOTH the key and the explicit
    allow flag, so an OSS install that does nothing has no usable vault."""
    raw = os.environ.get("AZTEA_VAULT_LOCAL_KEK", "").strip()
    if not raw or not feature_flags.flag("AZTEA_VAULT_ALLOW_LOCAL_KEK", default=False):
        return None
    try:
        key = base64.b64decode(raw)
    except (ValueError, TypeError) as exc:
        raise VaultError("AZTEA_VAULT_LOCAL_KEK is not valid base64.") from exc
    if len(key) != _LOCAL_KEK_BYTES:
        raise VaultError(f"AZTEA_VAULT_LOCAL_KEK must decode to {_LOCAL_KEK_BYTES} bytes.")
    return key


def _selected_scheme() -> str:
    """The configured KEK scheme. Raises VaultUnavailable when none is set so the
    caller fails closed instead of persisting under a weak scheme."""
    if _kms_key_id():
        return _SCHEME_KMS
    if _local_kek() is not None:
        return _SCHEME_LOCAL
    raise VaultUnavailable(
        "no KEK configured: set AZTEA_VAULT_KMS_KEY_ID (hosted) or "
        "AZTEA_VAULT_LOCAL_KEK + AZTEA_VAULT_ALLOW_LOCAL_KEK=1 (self-host)."
    )


def _wrap_dek(dek: bytes) -> tuple[bytes, str, str]:
    """Wrap a DEK with the configured KEK. Returns (wrapped_dek, enc_scheme, kek_ref)."""
    scheme = _selected_scheme()
    if scheme == _SCHEME_KMS:
        import boto3  # lazy: only the hosted path needs the AWS client
        out = boto3.client("kms").encrypt(KeyId=_kms_key_id(), Plaintext=dek)
        return bytes(out["CiphertextBlob"]), scheme, _kms_key_id()
    kek = _local_kek()
    if kek is None:  # pragma: no cover - _selected_scheme already guaranteed it
        raise VaultUnavailable("local KEK disappeared between select and wrap.")
    wrap_nonce = secrets.token_bytes(_NONCE_BYTES)
    wrapped = wrap_nonce + AESGCM(kek).encrypt(wrap_nonce, dek, _LOCAL_WRAP_AAD)
    return wrapped, scheme, "local:" + hashlib.sha256(kek).hexdigest()[:16]


def _unwrap_dek(wrapped: bytes, scheme: str, kek_ref: str) -> bytes:
    """Unwrap a DEK. Raises VaultUnavailable when the matching KEK is not configured."""
    if scheme == _SCHEME_KMS:
        import boto3
        out = boto3.client("kms").decrypt(CiphertextBlob=bytes(wrapped), KeyId=kek_ref or _kms_key_id())
        return bytes(out["Plaintext"])
    kek = _local_kek()
    if kek is None:
        raise VaultUnavailable("local KEK not configured; cannot unwrap this credential.")
    blob = bytes(wrapped)
    return AESGCM(kek).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], _LOCAL_WRAP_AAD)


# --------------------------------------------------------------------------- #
# Envelope encrypt / decrypt
# --------------------------------------------------------------------------- #


def _build_aad(*, credential_id: str, owner_id: str, domain: str, cred_kind: str, version: int) -> bytes:
    """The additional-authenticated-data bound into every ciphertext. Tying it to row
    identity means AES-GCM decryption fails if a row's ciphertext is swapped onto
    another row (different id/owner/domain/kind/version)."""
    return crypto.canonical_json({
        "v": _AAD_SCHEME,
        "credential_id": credential_id,
        "owner_id": owner_id,
        "domain": domain,
        "cred_kind": cred_kind,
        "version": int(version),
    })


def _encrypt_secret(secret: dict[str, Any], *, credential_id: str, owner_id: str,
                    domain: str, cred_kind: str, version: int) -> dict[str, Any]:
    """Envelope-encrypt one secret. Returns the columns to persist (no plaintext)."""
    dek = AESGCM.generate_key(_DEK_BITS)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    aad = _build_aad(credential_id=credential_id, owner_id=owner_id, domain=domain,
                     cred_kind=cred_kind, version=version)
    ciphertext = AESGCM(dek).encrypt(nonce, crypto.canonical_json(secret), aad)
    wrapped, scheme, kek_ref = _wrap_dek(dek)
    return {
        "wrapped_dek": wrapped, "kek_ref": kek_ref, "enc_scheme": scheme,
        "nonce": nonce, "ciphertext": ciphertext,
        "aad_fingerprint": hashlib.sha256(aad).hexdigest(),
    }


def _decrypt_row(row: dict[str, Any]) -> dict[str, Any]:
    """Decrypt a persisted row back to the plaintext secret dict."""
    aad = _build_aad(credential_id=row["credential_id"], owner_id=row["owner_id"],
                     domain=row["domain"], cred_kind=row["cred_kind"], version=row["version"])
    dek = _unwrap_dek(bytes(row["wrapped_dek"]), row["enc_scheme"], row["kek_ref"])
    plaintext = AESGCM(dek).decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]), aad)
    return json.loads(plaintext.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Validation + lifecycle
# --------------------------------------------------------------------------- #


def _validate_kind(cred_kind: str) -> str:
    if cred_kind not in _VALID_KINDS:
        raise VaultError(f"cred_kind must be one of {_VALID_KINDS}")
    return cred_kind


def _validate_secret(kind: str, secret: Any) -> None:
    """Fail loud at the boundary: a secret missing its required fields never gets
    encrypted-and-stored as a silently-useless row."""
    if not isinstance(secret, dict):
        raise VaultError("secret must be an object")
    missing = [f for f in _REQUIRED_FIELDS[kind] if not secret.get(f)]
    if missing:
        raise VaultError(f"{kind} credential missing required field(s): {', '.join(missing)}")


def _require_enabled() -> None:
    if not feature_flags.credential_vault_enabled():
        raise VaultUnavailable("credential vault disabled (AZTEA_CREDENTIAL_VAULT_ENABLED=0).")


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    """Pure: the safe-to-return view — no ciphertext, no DEK, no plaintext."""
    return {
        "credential_id": row["credential_id"],
        "domain": row["domain"],
        "label": row.get("label"),
        "cred_kind": row["cred_kind"],
        "status": row["status"],
        "version": row["version"],
        "last_used_at": row.get("last_used_at"),
        "created_at": row["created_at"],
    }


def store_credential(*, owner_id: str, domain: str, cred_kind: str,
                     secret: dict[str, Any], label: str | None = None) -> dict[str, Any]:
    """Encrypt + persist a credential. Returns METADATA ONLY (never the secret).

    Replaces any existing active credential for (owner, domain, kind) — the old row is
    revoked + crypto-shredded in the same transaction so the unique scope index holds.
    """
    _require_enabled()
    kind = _validate_kind(cred_kind)
    _validate_secret(kind, secret)
    dom = normalize_domain(domain)
    cred_id = _new_id()
    enc = _encrypt_secret(secret, credential_id=cred_id, owner_id=str(owner_id),
                          domain=dom, cred_kind=kind, version=1)
    now = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE website_credentials SET status='revoked', ciphertext=%s, wrapped_dek=%s, "
            "revoked_at=%s, updated_at=%s WHERE owner_id=%s AND domain=%s AND cred_kind=%s "
            "AND status='active'",
            (_SHRED, _SHRED, now, now, str(owner_id), dom, kind),
        )
        conn.execute(
            "INSERT INTO website_credentials (credential_id, owner_id, domain, label, cred_kind, "
            "enc_scheme, wrapped_dek, kek_ref, nonce, ciphertext, aad_fingerprint, status, version, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',1,%s,%s)",
            (cred_id, str(owner_id), dom, label, kind, enc["enc_scheme"], enc["wrapped_dek"],
             enc["kek_ref"], enc["nonce"], enc["ciphertext"], enc["aad_fingerprint"], now, now),
        )
    return _metadata({
        "credential_id": cred_id, "domain": dom, "label": label, "cred_kind": kind,
        "status": "active", "version": 1, "last_used_at": None, "created_at": now,
    })


def list_credentials(owner_id: str, *, domain: str | None = None) -> list[dict[str, Any]]:
    """Active credentials for an owner — METADATA ONLY (safe for any API response)."""
    _require_enabled()
    sql = ("SELECT credential_id, owner_id, domain, label, cred_kind, status, version, "
           "last_used_at, created_at FROM website_credentials WHERE owner_id=%s AND status='active'")
    params: list[Any] = [str(owner_id)]
    if domain:
        sql += " AND domain=%s"
        params.append(normalize_domain(domain))
    sql += " ORDER BY created_at DESC"
    with _conn() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_metadata(dict(r)) for r in rows]


def revoke_credential(credential_id: str, owner_id: str) -> bool:
    """Revoke + crypto-shred an active credential (overwrite ciphertext + wrapped DEK
    with empty bytes). Returns True iff exactly one active row was revoked."""
    _require_enabled()
    now = _now()
    with _conn() as conn:
        result = conn.execute(
            "UPDATE website_credentials SET status='revoked', ciphertext=%s, wrapped_dek=%s, "
            "revoked_at=%s, updated_at=%s WHERE credential_id=%s AND owner_id=%s AND status='active'",
            (_SHRED, _SHRED, now, now, credential_id, str(owner_id)),
        )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def rotate_credential(*, owner_id: str, domain: str, cred_kind: str,
                      new_secret: dict[str, Any]) -> dict[str, Any]:
    """Re-encrypt the active (owner, domain, kind) credential with a fresh DEK + nonce
    and a bumped version. Returns metadata only. Raises VaultError if none exists."""
    _require_enabled()
    kind = _validate_kind(cred_kind)
    _validate_secret(kind, new_secret)
    dom = normalize_domain(domain)
    with _conn() as conn:
        row = conn.execute(
            "SELECT credential_id, version FROM website_credentials WHERE owner_id=%s AND domain=%s "
            "AND cred_kind=%s AND status='active'",
            (str(owner_id), dom, kind),
        ).fetchone()
        if row is None:
            raise VaultError("no active credential to rotate for this owner/domain/kind.")
        new_version = int(row["version"]) + 1
        enc = _encrypt_secret(new_secret, credential_id=row["credential_id"], owner_id=str(owner_id),
                              domain=dom, cred_kind=kind, version=new_version)
        now = _now()
        conn.execute(
            "UPDATE website_credentials SET enc_scheme=%s, wrapped_dek=%s, kek_ref=%s, nonce=%s, "
            "ciphertext=%s, aad_fingerprint=%s, version=%s, updated_at=%s WHERE credential_id=%s",
            (enc["enc_scheme"], enc["wrapped_dek"], enc["kek_ref"], enc["nonce"], enc["ciphertext"],
             enc["aad_fingerprint"], new_version, now, row["credential_id"]),
        )
    return _metadata({
        "credential_id": row["credential_id"], "domain": dom, "label": None, "cred_kind": kind,
        "status": "active", "version": new_version, "last_used_at": None, "created_at": now,
    })


# --------------------------------------------------------------------------- #
# Decrypt-for-injection (write path only)
# --------------------------------------------------------------------------- #


def _mandate_domains(mandate: dict[str, Any]) -> list[str]:
    raw = mandate.get("allowed_domains")
    if isinstance(raw, list):
        return [str(d or "").strip().lower() for d in raw if d]
    try:
        decoded = json.loads(raw or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(d or "").strip().lower() for d in decoded if d] if isinstance(decoded, list) else []


def _host_in_allowed(host: str, allowed: list[str]) -> bool:
    """Pure: host equals one of allowed, or is a subdomain of one (mirrors
    web_actor._url_within_allowed_domains so the binding is identical)."""
    return bool(host) and any(host == d or host.endswith("." + d) for d in allowed)


def _decrypt_for_injection(*, owner_id: str, domain: str, cred_kind: str,
                           mandate: dict[str, Any]) -> Credential | None:
    """Decrypt the active credential for use in a live browser context. Internal: never
    exposed over HTTP. Returns None when no matching credential exists.

    Gates (every one must pass): injection flag on; the mandate belongs to the same
    owner; the requested domain is covered by the mandate's allowed_domains; the row is
    active and decrypts (AAD binds it to its identity). Stamps last_used_at on success.
    """
    if not feature_flags.credential_injection_enabled():
        raise VaultUnavailable("credential injection disabled (AZTEA_CREDENTIAL_INJECTION_ENABLED=0).")
    if str(mandate.get("caller_owner_id") or "") != str(owner_id):
        raise VaultError("mandate owner does not match the credential owner.")
    dom = normalize_domain(domain)
    if not _host_in_allowed(dom, _mandate_domains(mandate)):
        raise VaultError("requested domain is not covered by the mandate's allowed_domains.")
    kind = _validate_kind(cred_kind)
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM website_credentials WHERE owner_id=%s AND domain=%s AND cred_kind=%s "
            "AND status='active'",
            (str(owner_id), dom, kind),
        ).fetchone()
        if row is None:
            return None
        secret = _decrypt_row(dict(row))
        conn.execute(
            "UPDATE website_credentials SET last_used_at=%s WHERE credential_id=%s",
            (_now(), row["credential_id"]),
        )
    return Credential(kind=kind, data=secret)
