"""Unit tests for proof-of-observation receipts (Phase 2).

Covers issue->verify roundtrip, the tamper matrix (extraction, observed_at,
wrong key), server-stamped observed_at, and the DB roundtrip through verify.
The claim is provenance, not truth — verify says so.
"""

from __future__ import annotations

import pytest

from core import crypto
from core import observation_receipts as obs


@pytest.fixture()
def receipts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "DB_PATH", str(tmp_path / "obs.db"))
    obs.init_observation_receipts_db()
    return obs


def _issue(db, priv, did="did:web:host:agents:agent-x", extraction=None):
    return db.issue_observation_receipt(
        agent_id="agent-x", private_pem=priv, signer_did=did,
        request_url="https://example.com/p", final_url="https://example.com/p",
        dom_snapshot=b"<accessibility-tree-bytes>",
        extraction=extraction if extraction is not None else {"tiers": [1, 2]},
    )


def test_issue_and_verify_roundtrip(receipts_db):
    priv, pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv)
    assert r is not None and r["claim"] == "provenance_only"
    v = receipts_db.verify_receipt_object(r, pub)
    assert v["valid"] is True
    assert v["checks"]["signature_ok"] and v["checks"]["extraction_hash_ok"]


def test_observed_at_is_server_stamped_int(receipts_db):
    priv, _pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv)
    assert isinstance(r["observed_at"], int) and r["observed_at"] > 0


def test_tampered_extraction_fails_verification(receipts_db):
    priv, pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv, extraction={"price": 20})
    r["extraction"] = {"price": 0}  # swap the data after signing
    v = receipts_db.verify_receipt_object(r, pub)
    assert v["valid"] is False and v["checks"]["extraction_hash_ok"] is False


def test_tampered_observed_at_breaks_signature(receipts_db):
    priv, pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv)
    r["observed_at"] = int(r["observed_at"]) + 1
    v = receipts_db.verify_receipt_object(r, pub)
    assert v["valid"] is False and v["checks"]["signature_ok"] is False


def test_wrong_key_does_not_verify(receipts_db):
    priv, _pub = crypto.generate_signing_keypair()
    _other_priv, other_pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv)
    assert receipts_db.verify_receipt_object(r, other_pub)["valid"] is False


def test_persisted_row_roundtrips_through_verify(receipts_db):
    priv, pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv)
    row = receipts_db.get_observation_receipt(r["receipt_id"])
    assert row is not None
    rebuilt = receipts_db._row_to_receipt(row)
    assert receipts_db.verify_receipt_object(rebuilt, pub)["valid"] is True


def test_signer_did_spoof_is_rejected_when_real_did_supplied(receipts_db):
    # A caller signs with their OWN key (valid signature) but claims another agent's
    # identity in signer_did. verify must reject it once it knows the agent's real did.
    priv, pub = crypto.generate_signing_keypair()
    r = _issue(receipts_db, priv, did="did:web:host:agents:attacker")
    r["signer_did"] = "did:web:trusted-bank.com:agents:official"  # forge the claimed identity
    spoofed = receipts_db.verify_receipt_object(r, pub, expected_did="did:web:host:agents:attacker")
    assert spoofed["valid"] is False and spoofed["checks"]["did_ok"] is False
    assert spoofed["checks"]["signature_ok"] is True  # the signature is valid; the DID is the lie
    # When the claimed did matches the agent's real did, it verifies.
    r["signer_did"] = "did:web:host:agents:attacker"
    ok = receipts_db.verify_receipt_object(r, pub, expected_did="did:web:host:agents:attacker")
    assert ok["valid"] is True and ok["checks"]["did_ok"] is True
