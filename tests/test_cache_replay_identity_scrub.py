"""Cache replay must not leak the original caller's identity to a different caller.

Audit H-1 (2026-05-19): Aztea's cache is platform-wide. Without the scrub a
second caller hitting the same cache key receives an Ed25519-signed JWS whose
payload embeds the original caller's ``caller_owner_id`` ("user:UUID").
Cryptographic provenance over a cross-tenant identity leak is strictly worse
than a plain identity leak — it gives the leak an attestation. These tests pin
the scrub: same-caller cache hits still receive the original receipt; cross-
caller hits get the receipt dropped and a structured note.
"""
from __future__ import annotations

from unittest.mock import patch

import server.application as _app


_cache_hit = _app._cache_hit_response_payload


def _stub_lookup(mapping: dict[str, str | None]):
    """Patch _lookup_original_caller_id to return canned values keyed by job_id."""
    return patch.object(
        _app,
        "_lookup_original_caller_id",
        side_effect=lambda jid: mapping.get(str(jid)),
    )


def _stub_build_envelope(envelope: dict | None):
    return patch("core.receipts.build_receipt_envelope", return_value=envelope)


def test_same_caller_cache_hit_keeps_original_receipt():
    cached = {
        "_cached_job_id": "job-original",
        "findings": [],
        "summary": "Clean.",
    }
    envelope = {
        "jws": "header.payload.signature",
        "kid": "did:web:aztea.ai:agents:abc",
        "agent_id": "abc",
    }
    with _stub_lookup({"job-original": "user:alice"}), _stub_build_envelope(envelope):
        out = _cache_hit(cached, current_caller_id="user:alice")
    assert out["receipt_summary"] == "verified_via_cache"
    assert "signed_receipt" in out
    assert out["signed_receipt"]["via"] == "cache_replay"


def test_cross_caller_cache_hit_drops_receipt():
    cached = {
        "_cached_job_id": "job-original",
        "findings": [],
        "summary": "Clean.",
    }
    envelope_with_leak = {
        "jws": "eyJ...payload-containing-alice-user-id...sig",
        "kid": "did:web:aztea.ai:agents:abc",
        "agent_id": "abc",
    }
    with _stub_lookup({"job-original": "user:alice"}), _stub_build_envelope(envelope_with_leak):
        out = _cache_hit(cached, current_caller_id="user:eve")
    assert out["receipt_summary"] == "cross_tenant_cache_replay"
    assert "signed_receipt" not in out
    assert "receipt" not in out
    assert (
        out["cache"]["receipt_omitted_reason"]
        == "cross_tenant_cache_replay_identity_scrub"
    )


def test_unknown_original_caller_treated_as_cross_tenant():
    # If we can't resolve the original caller (DB hiccup, deleted job), we must
    # treat the replay as cross-tenant — better to drop the receipt than to
    # leak an identity we can't verify.
    cached = {"_cached_job_id": "job-original", "ok": True}
    with _stub_lookup({"job-original": None}), _stub_build_envelope({"jws": "x.y.z"}):
        out = _cache_hit(cached, current_caller_id="user:eve")
    assert out["receipt_summary"] == "cross_tenant_cache_replay"
    assert "signed_receipt" not in out


def test_no_original_job_id_in_cached_payload():
    # Edge case: cache row without _cached_job_id (legacy entries). Don't
    # crash; mark the receipt absent.
    cached = {"ok": True}
    out = _cache_hit(cached, current_caller_id="user:alice")
    assert out["receipt_summary"] == "absent_no_origin_job"
    assert "signed_receipt" not in out
