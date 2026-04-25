"""Unit tests for core.crypto Ed25519 signing primitives."""

from __future__ import annotations

import base64
import json

import pytest

from core import crypto


def test_generate_signing_keypair_returns_pem_strings():
    private_pem, public_pem = crypto.generate_signing_keypair()
    assert private_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert public_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert "ENCRYPTED" not in private_pem
    # Two generations should produce different keys.
    private2, public2 = crypto.generate_signing_keypair()
    assert private_pem != private2
    assert public_pem != public2


def test_canonical_json_is_deterministic_regardless_of_key_order():
    a = {"foo": 1, "bar": 2, "nested": {"y": True, "x": False}}
    b = {"nested": {"x": False, "y": True}, "bar": 2, "foo": 1}
    assert crypto.canonical_json(a) == crypto.canonical_json(b)


def test_canonical_json_contains_no_whitespace_or_unicode_escapes():
    payload = {"text": "héllo", "n": 1}
    assert crypto.canonical_json(payload) == b'{"n":1,"text":"h\xc3\xa9llo"}'


def test_sign_then_verify_succeeds():
    private_pem, public_pem = crypto.generate_signing_keypair()
    payload = {"job_id": "abc", "result": [1, 2, 3], "ok": True}
    signature = crypto.sign_payload(private_pem, payload)
    assert crypto.verify_signature(public_pem, payload, signature) is True


def test_verify_fails_for_modified_payload():
    private_pem, public_pem = crypto.generate_signing_keypair()
    payload = {"a": 1}
    signature = crypto.sign_payload(private_pem, payload)
    assert crypto.verify_signature(public_pem, {"a": 2}, signature) is False


def test_verify_fails_for_wrong_key():
    private_pem, _ = crypto.generate_signing_keypair()
    _, other_public_pem = crypto.generate_signing_keypair()
    payload = {"x": 1}
    signature = crypto.sign_payload(private_pem, payload)
    assert crypto.verify_signature(other_public_pem, payload, signature) is False


def test_verify_returns_false_on_garbage_signature():
    _, public_pem = crypto.generate_signing_keypair()
    assert crypto.verify_signature(public_pem, {"x": 1}, "not-base-64!!!") is False
    assert crypto.verify_signature(public_pem, {"x": 1}, "QUJD") is False  # valid b64, wrong length


def test_verify_returns_false_on_garbage_public_key():
    private_pem, _ = crypto.generate_signing_keypair()
    payload = {"x": 1}
    signature = crypto.sign_payload(private_pem, payload)
    assert crypto.verify_signature("-----BEGIN PUBLIC KEY-----\nGARBAGE\n-----END PUBLIC KEY-----", payload, signature) is False


def test_jwk_export_has_correct_shape():
    _, public_pem = crypto.generate_signing_keypair()
    jwk = crypto.public_key_to_jwk(public_pem)
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    # x must decode back to exactly 32 bytes (Ed25519 raw public key length).
    raw = base64.urlsafe_b64decode(jwk["x"] + "==")
    assert len(raw) == 32


def test_sign_payload_rejects_non_ed25519_pem():
    # Pass an obviously wrong key and confirm we don't silently succeed.
    with pytest.raises((ValueError, Exception)):
        crypto.sign_payload("not-a-pem", {"x": 1})


def test_verify_signature_handles_lists_and_primitives():
    private_pem, public_pem = crypto.generate_signing_keypair()
    for payload in ([1, 2, 3], "hello", 42, None, True):
        signature = crypto.sign_payload(private_pem, payload)
        assert crypto.verify_signature(public_pem, payload, signature) is True


def test_canonical_json_round_trip_via_json_module():
    # The canonical bytes must be valid JSON that round-trips losslessly.
    payload = {"a": 1, "b": [1, "two", None], "c": {"d": True}}
    encoded = crypto.canonical_json(payload)
    assert json.loads(encoded) == payload
