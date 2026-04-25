"""Ed25519 signing primitives for agent cryptographic identity.

Every agent registered on Aztea gets an Ed25519 keypair (see
``register_agent`` in ``core/registry/agents_ops.py``). When the agent
completes a job, the platform signs the output payload on the agent's
behalf using its private key. Any external party can fetch the agent's
DID document — which contains the public key — and independently verify
any signed output without trusting Aztea.

Design notes:

- **Canonical JSON.** The bytes that get signed are produced by
  :func:`canonical_json`. Signing the canonical form (not the original
  serialization) means a verifier can re-serialize the payload they
  fetched back from ``GET /jobs/{id}`` and produce the same bytes,
  regardless of the JSON library or key ordering used at HTTP encode time.
- **Raw signature bytes, base64-encoded.** Ed25519 signatures are 64
  bytes; we don't wrap them in DER. The base64 encoding is for
  HTTP/JSON transport.
- **PEM I/O.** Keys are stored as PEM strings on the agents table for
  consistency with how the rest of the codebase handles structured
  secrets. We never expose the private PEM over HTTP.
- **PKCS#8 / SubjectPublicKeyInfo formats** are the standard, widely
  interoperable wrappers — anything that can read a PEM Ed25519 key can
  read these.
"""

from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature


_PEM = serialization.Encoding.PEM
_PRIVATE_FMT = serialization.PrivateFormat.PKCS8
_PUBLIC_FMT = serialization.PublicFormat.SubjectPublicKeyInfo
_NO_ENC = serialization.NoEncryption()


def generate_signing_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair.

    Returns ``(private_pem, public_pem)`` — both ASCII PEM strings.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(_PEM, _PRIVATE_FMT, _NO_ENC).decode("ascii")
    public_pem = private_key.public_key().public_bytes(_PEM, _PUBLIC_FMT).decode("ascii")
    return private_pem, public_pem


def canonical_json(payload: dict | list | str | int | float | bool | None) -> bytes:
    """Deterministic JSON encoding used as the signing input.

    ``sort_keys=True`` and the compact ``separators`` mean that any
    JSON-equivalent value produces the same bytes regardless of insertion
    order or whitespace.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_payload(private_pem: str, payload) -> str:
    """Sign ``canonical_json(payload)`` with the given Ed25519 private key.

    ``private_pem`` must be the PEM produced by :func:`generate_signing_keypair`
    (or any PKCS#8 PEM Ed25519 key). Returns the base64-encoded raw
    signature (88 characters).
    """
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("private_pem must be an Ed25519 PEM key.")
    signature_bytes = key.sign(canonical_json(payload))
    return base64.b64encode(signature_bytes).decode("ascii")


def verify_signature(public_pem: str, payload, signature_b64: str) -> bool:
    """Return True iff ``signature_b64`` is a valid Ed25519 signature
    over ``canonical_json(payload)`` for the given public key.
    """
    try:
        key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    except (ValueError, TypeError):
        return False
    if not isinstance(key, ed25519.Ed25519PublicKey):
        return False
    try:
        signature_bytes = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        key.verify(signature_bytes, canonical_json(payload))
    except InvalidSignature:
        return False
    except Exception:
        return False
    return True


def public_key_to_jwk(public_pem: str) -> dict:
    """Return the public key as a JWK per RFC 8037 (OKP / Ed25519).

    Used inside the DID document's ``verificationMethod[].publicKeyJwk``.
    """
    key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ValueError("public_pem must be an Ed25519 PEM key.")
    raw_bytes = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    x_b64url = base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")
    return {"kty": "OKP", "crv": "Ed25519", "x": x_b64url}
