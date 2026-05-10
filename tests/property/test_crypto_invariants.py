"""Hypothesis property tests for core.crypto and core.identity.

# OWNS: invariants on canonical_json, generate_signing_keypair, sign_payload,
#       verify_signature, public_key_to_jwk, build_agent_did, did_document_url.
# INVARIANTS asserted: sign/verify roundtrip; verify never raises on bad input;
#       canonical_json deterministic + idempotent; jwk has the right shape;
#       did:web format is stable.
"""
from __future__ import annotations

import base64
import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.crypto import (
    canonical_json,
    generate_signing_keypair,
    public_key_to_jwk,
    sign_payload,
    verify_signature,
)
from core.identity import build_agent_did, did_document_url
from tests.strategies import json_value

pytestmark = pytest.mark.property


# Generating an Ed25519 keypair on every example is too slow; share one
# keypair via a module-scoped fixture-equivalent. Hypothesis is fine with
# module globals as long as the test is otherwise pure.
_PRIVATE_PEM, _PUBLIC_PEM = generate_signing_keypair()


# --- canonical_json ----------------------------------------------------------

@given(payload=json_value())
def test_canonical_json_returns_bytes(payload):
    out = canonical_json(payload)
    assert isinstance(out, bytes)
    # Must be valid UTF-8 JSON.
    decoded = json.loads(out.decode("utf-8"))
    assert decoded == payload or _floats_equal(decoded, payload)


@given(payload=json_value())
def test_canonical_json_deterministic(payload):
    a = canonical_json(payload)
    b = canonical_json(payload)
    assert a == b


@given(payload=json_value())
def test_canonical_json_idempotent_via_json_loads(payload):
    """Encoding, decoding, and re-encoding produces identical bytes."""
    once = canonical_json(payload)
    twice = canonical_json(json.loads(once.decode("utf-8")))
    assert once == twice


def test_canonical_json_dict_key_order_independent():
    """Two dicts with same content but different insertion order canonicalize identically."""
    a = {"x": 1, "y": 2, "z": 3}
    b = {"z": 3, "y": 2, "x": 1}
    assert canonical_json(a) == canonical_json(b)


# --- sign / verify roundtrip -------------------------------------------------

@given(payload=json_value())
@settings(suppress_health_check=[HealthCheck.too_slow])
def test_sign_verify_roundtrip(payload):
    sig = sign_payload(_PRIVATE_PEM, payload)
    assert verify_signature(_PUBLIC_PEM, payload, sig) is True


@given(payload=json_value())
def test_signature_is_base64(payload):
    sig = sign_payload(_PRIVATE_PEM, payload)
    raw = base64.b64decode(sig)
    assert len(raw) == 64  # Ed25519 raw signature is 64 bytes


@given(payload=json_value(), tampered_field=st.text(min_size=1, max_size=4))
def test_verify_rejects_tampered_payload(payload, tampered_field):
    sig = sign_payload(_PRIVATE_PEM, payload)
    if isinstance(payload, dict):
        tampered = dict(payload)
        tampered[tampered_field] = "tamper"
    else:
        tampered = [payload, tampered_field]
    if canonical_json(tampered) == canonical_json(payload):
        return  # pathological case where tampering happens to be a no-op
    assert verify_signature(_PUBLIC_PEM, tampered, sig) is False


@given(garbage=st.text(min_size=0, max_size=80))
def test_verify_never_raises_on_bad_pem(garbage):
    """verify_signature returns False on malformed PEM, never raises."""
    result = verify_signature(garbage, {"x": 1}, "AAAA")
    assert result is False


@given(garbage=st.text(min_size=0, max_size=80))
def test_verify_never_raises_on_bad_signature(garbage):
    result = verify_signature(_PUBLIC_PEM, {"x": 1}, garbage)
    assert result is False


# --- keypair generation ------------------------------------------------------

def test_keypair_yields_two_pem_strings():
    priv, pub = generate_signing_keypair()
    assert priv.startswith("-----BEGIN PRIVATE KEY-----")
    assert pub.startswith("-----BEGIN PUBLIC KEY-----")


def test_keypair_uniqueness():
    a = generate_signing_keypair()
    b = generate_signing_keypair()
    assert a != b


# --- jwk ---------------------------------------------------------------------

def test_jwk_shape():
    jwk = public_key_to_jwk(_PUBLIC_PEM)
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert "x" in jwk
    raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    assert len(raw) == 32  # Ed25519 raw public key is 32 bytes


def test_jwk_is_deterministic():
    a = public_key_to_jwk(_PUBLIC_PEM)
    b = public_key_to_jwk(_PUBLIC_PEM)
    assert a == b


# --- did:web -----------------------------------------------------------------

agent_id_strategy = st.uuids().map(str)
base_url_strategy = st.sampled_from([
    "http://localhost:8000",
    "https://aztea.ai",
    "https://staging.aztea.ai",
    "http://127.0.0.1:8000",
    None,
])


@given(agent_id=agent_id_strategy, base_url=base_url_strategy)
def test_build_agent_did_format(agent_id, base_url):
    did = build_agent_did(agent_id, server_base_url=base_url)
    assert did.startswith("did:web:")
    assert agent_id in did
    parts = did.split(":")
    assert parts[0] == "did" and parts[1] == "web"


@given(agent_id=agent_id_strategy)
def test_build_agent_did_deterministic(agent_id):
    a = build_agent_did(agent_id, server_base_url="https://example.com")
    b = build_agent_did(agent_id, server_base_url="https://example.com")
    assert a == b


@given(agent_id=agent_id_strategy, base_url=base_url_strategy)
def test_did_document_url_resolves(agent_id, base_url):
    url = did_document_url(agent_id, server_base_url=base_url)
    assert url.startswith(("http://", "https://"))
    assert agent_id in url
    assert url.endswith(f"/agents/{agent_id}/did.json")


# --- helpers -----------------------------------------------------------------

def _floats_equal(a, b) -> bool:
    """JSON loses int/float distinction in some cases; normalize for comparison."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_floats_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_floats_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return False
    return a == b
