"""Plan B Phase 1 (2026-05-27) — HMAC signing of outbound agent calls.

Covers:
  1. `core.crypto.generate_endpoint_signing_secret` produces 256-bit URL-safe secrets.
  2. `sign_endpoint_request` + `verify_endpoint_request` round-trip cleanly.
  3. The verifier rejects (a) wrong signature, (b) stale timestamp, (c) wrong secret.
  4. The SDK helper `aztea.verify.verify_request` mirrors the same contract.
  5. Registration assigns a secret for http(s):// endpoints, skips internal://
     and skill:// endpoints (Aztea-hosted, no outbound call).
  6. Rotation generates a new secret distinct from the old one.

Outbound-call integration (the actual HMAC header on the request to a seller's
URL) is exercised end-to-end by the integration test in
`tests/integration/test_endpoint_signing_integration.py` (TBD) which spins up
a tiny verifying receiver. This file is the unit-level pin.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from core import crypto
from core.crypto import (
    ENDPOINT_SIGNATURE_MAX_AGE_SECONDS,
    generate_endpoint_signing_secret,
    sign_endpoint_request,
    verify_endpoint_request,
)


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------


def test_generate_endpoint_signing_secret_has_high_entropy():
    secrets_seen = {generate_endpoint_signing_secret() for _ in range(50)}
    # 256-bit URL-safe base64 → 43 chars, no padding.
    assert all(len(s) == 43 for s in secrets_seen)
    # All 50 distinct = no obvious collisions.
    assert len(secrets_seen) == 50


def test_sign_then_verify_roundtrip():
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    ts = "2026-05-27T20:15:30Z"
    sig = sign_endpoint_request(body, secret, ts)
    assert sig.startswith("sha256=")
    # Round-trip with the same now_epoch as the timestamp = always inside the window.
    verify_endpoint_request(
        body, sig, ts, secret, now_epoch=crypto._parse_iso_or_epoch(ts),
    )


def test_verify_rejects_wrong_signature():
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    ts = "2026-05-27T20:15:30Z"
    sig = sign_endpoint_request(body, secret, ts)
    tampered = sig[:-1] + ("a" if sig[-1] != "a" else "b")
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        verify_endpoint_request(
            body, tampered, ts, secret,
            now_epoch=crypto._parse_iso_or_epoch(ts),
        )


def test_verify_rejects_wrong_secret():
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    ts = "2026-05-27T20:15:30Z"
    sig = sign_endpoint_request(body, secret, ts)
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        verify_endpoint_request(
            body, sig, ts, generate_endpoint_signing_secret(),
            now_epoch=crypto._parse_iso_or_epoch(ts),
        )


def test_verify_rejects_stale_timestamp():
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    ts = "2026-05-27T20:15:30Z"
    sig = sign_endpoint_request(body, secret, ts)
    from cryptography.exceptions import InvalidSignature
    # Pretend "now" is 1 hour after the signed timestamp.
    stale_now = crypto._parse_iso_or_epoch(ts) + 3600
    with pytest.raises(InvalidSignature):
        verify_endpoint_request(body, sig, ts, secret, now_epoch=stale_now)


def test_verify_rejects_tampered_body():
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    ts = "2026-05-27T20:15:30Z"
    sig = sign_endpoint_request(body, secret, ts)
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        verify_endpoint_request(
            b'{"task":"goodbye"}', sig, ts, secret,
            now_epoch=crypto._parse_iso_or_epoch(ts),
        )


def test_max_age_constant_is_five_minutes():
    """5 minutes is the contract the SDK ships with; widening it weakens replay defence."""
    assert ENDPOINT_SIGNATURE_MAX_AGE_SECONDS == 300


def test_verify_rejects_nan_timestamp():
    """Audit fix 2026-05-27: float('nan') passes float() but abs(now - nan) > window
    evaluates False, silently bypassing the staleness check. Reject explicitly."""
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    sig = sign_endpoint_request(body, secret, "1234567890")
    from cryptography.exceptions import InvalidSignature
    for nasty in ("nan", "inf", "-inf", "NaN", "Infinity"):
        with pytest.raises(InvalidSignature):
            verify_endpoint_request(body, sig, nasty, secret, now_epoch=1234567890.0)


def test_sdk_verify_rejects_nan_timestamp():
    """Same NaN/Inf guard in the SDK helper."""
    import sys
    sdk_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "sdks", "python-sdk",
    )
    sys.path.insert(0, sdk_root)
    try:
        if "aztea.verify" in sys.modules:
            del sys.modules["aztea.verify"]
        from aztea.verify import verify_request, InvalidSignature
    finally:
        sys.path.remove(sdk_root)
    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    sig = sign_endpoint_request(body, secret, "1234567890")
    for nasty in ("nan", "inf"):
        with pytest.raises(InvalidSignature):
            verify_request(body, sig, nasty, secret, now_epoch=1234567890.0)


# ---------------------------------------------------------------------------
# SDK helper (aztea.verify.verify_request)
# ---------------------------------------------------------------------------


def test_sdk_verify_request_mirrors_server_signature():
    """The SDK helper must accept exactly what the server signs.

    This pins the cross-package contract — if either side drifts, the SDK
    helper would start rejecting legitimate calls.
    """
    import sys
    sdk_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "sdks", "python-sdk",
    )
    sys.path.insert(0, sdk_root)
    try:
        # Force re-import in case another test loaded a stale copy.
        if "aztea.verify" in sys.modules:
            del sys.modules["aztea.verify"]
        from aztea.verify import verify_request, InvalidSignature
    finally:
        sys.path.remove(sdk_root)

    secret = generate_endpoint_signing_secret()
    body = b'{"task":"hello"}'
    ts = "2026-05-27T20:15:30Z"
    sig = sign_endpoint_request(body, secret, ts)
    # Server signs, SDK verifies — must succeed.
    verify_request(body, sig, ts, secret, now_epoch=crypto._parse_iso_or_epoch(ts))
    # Wrong secret → InvalidSignature.
    with pytest.raises(InvalidSignature):
        verify_request(
            body, sig, ts, generate_endpoint_signing_secret(),
            now_epoch=crypto._parse_iso_or_epoch(ts),
        )


# ---------------------------------------------------------------------------
# Registration assigns the secret
# ---------------------------------------------------------------------------


def test_register_agent_assigns_endpoint_signing_secret(monkeypatch, tmp_path):
    """Every new http(s):// agent gets a 43-char secret persisted on the row."""
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    from core import registry
    aid = f"test-phase1-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"Agent_{aid}",
        description="phase1 unit test",
        endpoint_url="https://example.com/run",
        price_per_call_usd=0.05,
        tags=["test"],
        input_schema={"type": "object", "properties": {"task": {"type": "string"}}},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    agent = registry.get_agent(aid, include_unapproved=True)
    secret = agent.get("endpoint_signing_secret")
    assert isinstance(secret, str)
    assert len(secret) == 43
    assert agent.get("endpoint_signing_secret_rotated_at")


def test_register_agent_skips_secret_for_internal_endpoint(monkeypatch):
    """internal:// agents are Aztea-hosted; no outbound HTTP, no HMAC secret."""
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    from core import registry
    aid = f"test-internal-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"Internal_{aid}",
        description="internal skip test",
        endpoint_url="internal://some-builtin",
        price_per_call_usd=0.0,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    agent = registry.get_agent(aid, include_unapproved=True)
    assert agent.get("endpoint_signing_secret") is None


def test_register_agent_skips_secret_for_skill_endpoint(monkeypatch):
    """skill:// agents are Aztea-hosted (LLM prompt runtime); no HMAC secret."""
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    from core import registry
    aid = f"test-skill-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"Skill_{aid}",
        description="skill scheme skip test",
        endpoint_url=f"skill://my-skill-{uuid.uuid4().hex[:8]}",
        price_per_call_usd=0.02,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    agent = registry.get_agent(aid, include_unapproved=True)
    assert agent.get("endpoint_signing_secret") is None


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotate_endpoint_signing_secret_changes_value(monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    from core import registry
    aid = f"test-rotate-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"Rotate_{aid}",
        description="rotation test",
        endpoint_url="https://example.com/run",
        price_per_call_usd=0.05,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    original = registry.get_agent(aid, include_unapproved=True)["endpoint_signing_secret"]
    new_secret = registry.rotate_endpoint_signing_secret(aid)
    assert new_secret is not None
    assert new_secret != original
    # The new secret is now on the row.
    refetched = registry.get_agent(aid, include_unapproved=True)
    assert refetched["endpoint_signing_secret"] == new_secret


def test_rotate_endpoint_signing_secret_returns_none_for_internal(monkeypatch):
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    from core import registry
    aid = f"test-rotate-internal-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"RotateInternal_{aid}",
        description="rotation no-op for internal://",
        endpoint_url="internal://some-builtin",
        price_per_call_usd=0.0,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    assert registry.rotate_endpoint_signing_secret(aid) is None


# ---------------------------------------------------------------------------
# _agent_response scrub list
# ---------------------------------------------------------------------------


def test_agent_response_scrubs_endpoint_signing_secret(monkeypatch):
    """The secret leaks ONLY through registration/rotation. Catalog reads must scrub it."""
    monkeypatch.setenv("AZTEA_SKIP_REGISTER_ENDPOINT_PROBE", "1")
    from core import registry
    # part_002 is a shard — depends on symbols from part_000. Import via the
    # composed application namespace, not the shard directly.
    import server.application as _server_app
    _agent_response = _server_app._agent_response

    aid = f"test-scrub-{uuid.uuid4().hex[:8]}"
    registry.register_agent(
        name=f"Scrub_{aid}",
        description="scrub test",
        endpoint_url="https://example.com/run",
        price_per_call_usd=0.05,
        tags=["test"],
        input_schema={"type": "object"},
        owner_id=f"user:{uuid.uuid4().hex[:8]}",
        embed_listing=False,
        agent_id=aid,
    )
    raw = registry.get_agent(aid, include_unapproved=True)
    assert raw.get("endpoint_signing_secret")  # present at the data layer
    scrubbed = _agent_response(raw, caller={"type": "master", "owner_id": "user:x"})
    assert "endpoint_signing_secret" not in scrubbed
    assert "endpoint_signing_secret_rotated_at" not in scrubbed
