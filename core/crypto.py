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
import hashlib
import hmac
import json
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

_PEM = serialization.Encoding.PEM
_PRIVATE_FMT = serialization.PrivateFormat.PKCS8
_PUBLIC_FMT = serialization.PublicFormat.SubjectPublicKeyInfo
_NO_ENC = serialization.NoEncryption()

# Token length for endpoint-signing secrets. 32 bytes from secrets.token_urlsafe
# yields a 43-character URL-safe base64 string with ~256 bits of entropy.
_ENDPOINT_SIGNING_SECRET_BYTES = 32

# Replay window for inbound signatures the seller verifies. Five minutes is
# long enough to absorb clock skew and slow-network retries, short enough that
# a captured signature can't be replayed indefinitely.
ENDPOINT_SIGNATURE_MAX_AGE_SECONDS = 300


def generate_signing_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 keypair.

    Returns ``(private_pem, public_pem)`` — both ASCII PEM strings.
    """
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(_PEM, _PRIVATE_FMT, _NO_ENC).decode("ascii")
    public_pem = (
        private_key.public_key().public_bytes(_PEM, _PUBLIC_FMT).decode("ascii")
    )
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

    NOTE: prefer :func:`sign_output_v2` for new code — the v1 form signs
    only the output bytes, so the same output across two different
    ``job_id``s produces an identical signature (audit 2026-05-16 #5).
    """
    key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError("private_pem must be an Ed25519 PEM key.")
    signature_bytes = key.sign(canonical_json(payload))
    return base64.b64encode(signature_bytes).decode("ascii")


# Audit 2026-05-16 #5: bind every signature to (job_id, agent_id, output)
# so a signature minted for job A cannot be replayed onto a forged job B
# whose output happens to canonicalise to the same bytes. The string
# encodes the binding clearly so an offline verifier knows exactly what
# domain the signature speaks to.
OUTPUT_SIG_SCHEME_V2 = "Ed25519+aztea-output-sig/2"


def build_output_sigil(job_id: str, agent_id: str, output) -> dict:
    """Construct the canonical dict that v2 output signatures cover.

    Why a dict and not raw bytes: keeping the binding fields explicit
    lets verifiers fail loudly if anyone forwards a signature against a
    different job_id or agent_id. ``output_hash`` is computed here (over
    the canonicalised output) so the sigil itself stays compact.
    """
    import hashlib

    output_hash = hashlib.sha256(canonical_json(output)).hexdigest()
    return {
        "v": "aztea/output-sig/2",
        "job_id": str(job_id),
        "agent_id": str(agent_id),
        "output_hash": output_hash,
    }


def sign_output_v2(private_pem: str, job_id: str, agent_id: str, output) -> str:
    """Sign the v2 sigil (job_id + agent_id + output_hash) with Ed25519.

    Pair with :data:`OUTPUT_SIG_SCHEME_V2` when persisting alongside
    ``output_signature_alg`` so verifiers can route to the right path.
    """
    return sign_payload(private_pem, build_output_sigil(job_id, agent_id, output))


def verify_output_v2(
    public_pem: str,
    job_id: str,
    agent_id: str,
    output,
    signature_b64: str,
) -> bool:
    return verify_signature(
        public_pem, build_output_sigil(job_id, agent_id, output), signature_b64
    )


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


# ---------------------------------------------------------------------------
# Endpoint-signing secrets (Aztea -> seller HMAC)
# ---------------------------------------------------------------------------
#
# Why a separate primitive from the Ed25519 receipt keys above: the receipts
# go OUTBOUND to buyers who must verify them with the agent's public key
# (asymmetric). Endpoint requests go OUTBOUND to sellers who verify them with
# a shared secret they were given at registration (symmetric, HMAC). Sellers
# can't have private keys we control; shared secrets are the right shape for
# the wrapper-side verifier in 20 lines of code.
#
# The header convention mirrors core/watchers/delivery.py (already shipped for
# job callbacks): ``X-Aztea-Signature: sha256=<hex>`` over the request body,
# bound to ``X-Aztea-Timestamp`` so the seller can reject replays past the
# ``ENDPOINT_SIGNATURE_MAX_AGE_SECONDS`` window.


def generate_endpoint_signing_secret() -> str:
    """Pure: return a fresh URL-safe base64 secret with ~256 bits of entropy.

    Used at agent registration and on rotate. The string is human-copyable,
    URL-safe, and never contains padding characters.
    """
    return secrets.token_urlsafe(_ENDPOINT_SIGNING_SECRET_BYTES)


def sign_endpoint_request(body: bytes, secret: str, timestamp: str) -> str:
    """Pure: compute the ``X-Aztea-Signature`` value for an outbound call.

    The signed string is ``f"{timestamp}.{body_bytes}"`` so a captured
    signature can't be replayed against a different body OR against the same
    body at a different time. Returns ``"sha256=<hex>"``.

    Mirror the call-side check in ``verify_endpoint_request`` below.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes")
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    if not isinstance(timestamp, str) or not timestamp:
        raise ValueError("timestamp must be a non-empty string")
    signed_string = timestamp.encode("ascii") + b"." + bytes(body)
    mac = hmac.new(secret.encode("utf-8"), signed_string, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify_endpoint_request(
    body: bytes,
    signature: str,
    timestamp: str,
    secret: str,
    *,
    max_age_seconds: int = ENDPOINT_SIGNATURE_MAX_AGE_SECONDS,
    now_epoch: float | None = None,
) -> None:
    """Side-effect-free: raise on bad signature, stale timestamp, or wrong shape.

    Symmetric counterpart to :func:`sign_endpoint_request`. Used by the SDK
    wrapper helper (``sdks/python-sdk/aztea/verify.py``) and by Aztea's own
    inbound verification on rotation-secret flows.

    Raises ``InvalidSignature`` on any mismatch, including unparseable
    timestamps. Constant-time compare prevents timing oracles on the HMAC.
    """
    if not isinstance(signature, str) or not signature.startswith("sha256="):
        raise InvalidSignature("signature header missing or wrong format")
    if not isinstance(timestamp, str) or not timestamp:
        raise InvalidSignature("timestamp header missing")
    # Reject stale timestamps before doing HMAC math.
    try:
        ts_epoch = _parse_iso_or_epoch(timestamp)
    except ValueError as exc:
        raise InvalidSignature(f"timestamp not parseable: {exc}") from exc
    current = now_epoch if now_epoch is not None else time.time()
    if abs(current - ts_epoch) > max_age_seconds:
        raise InvalidSignature(
            f"timestamp outside +/-{max_age_seconds}s window"
        )
    expected = sign_endpoint_request(body, secret, timestamp)
    if not hmac.compare_digest(expected, signature):
        raise InvalidSignature("HMAC mismatch")


def _parse_iso_or_epoch(timestamp: str) -> float:
    """Pure: convert an ISO-8601 string or epoch-seconds string to a float.

    Accepts both ``"2026-05-27T20:15:30Z"`` and ``"1748382930"`` so wrappers
    don't have to standardize on one form. The Aztea sender always emits the
    ISO form; the dual-parse is defence-in-depth for buggy SDKs.
    """
    stripped = timestamp.strip()
    if not stripped:
        raise ValueError("empty timestamp")
    # Epoch-seconds path (digits + optional fractional component).
    # 2026-05-27 audit fix: reject NaN/Inf — float() accepts "nan"/"inf" and
    # abs(now - nan) > window evaluates False, silently bypassing the
    # staleness check. Real timestamps are always finite.
    try:
        candidate = float(stripped)
    except ValueError:
        pass
    else:
        import math
        if not math.isfinite(candidate):
            raise ValueError("non-finite timestamp")
        return candidate
    # ISO-8601 path. datetime.fromisoformat accepts Z suffix only on Python 3.11+;
    # the project is on 3.12 (per CLAUDE.md) so this is safe.
    from datetime import datetime, timezone
    try:
        # Replace trailing Z with +00:00 for older fromisoformat behaviour.
        normalized = stripped[:-1] + "+00:00" if stripped.endswith("Z") else stripped
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(str(exc))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
