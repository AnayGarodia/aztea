"""
jwt_debugger.py — Decode and security-analyse a raw JWT without a JWT library.

# OWNS: base64url decoding of JWT header/payload, claim validation (exp, nbf),
#        HMAC verification (HS256/HS384/HS512), RSA/EC verification via
#        `cryptography`, and algorithm-confusion risk detection.
# NOT OWNS: key management, token revocation, session lifecycle, network calls.
# INVARIANTS:
#   * Never raise — every code path returns a dict (success or error envelope).
#   * Never echo the raw token, secret, or private key in the response.
#   * `signature_valid = None` means "not checked" (no key provided).
#     `verified = True` only when a key was provided AND the signature matched.
#   * Decoding uses only stdlib; `cryptography` is imported lazily for RSA/EC.
# DECISIONS:
#   * Raw base64url + json instead of PyJWT — surfaces literal bytes even on
#     malformed or non-standard tokens, which is the point of this tool.
#   * `alg_confusion_risk` is conservative: only `none` and HS*/RSA-key mismatch
#     are flagged; other edge cases require server-side context we don't have.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

from agents._contracts import agent_error as _err

_LOG = logging.getLogger(__name__)

# HMAC digest map: alg name → hashlib digest constructor
_HMAC_ALGS: dict[str, Any] = {
    "HS256": hashlib.sha256,
    "HS384": hashlib.sha384,
    "HS512": hashlib.sha512,
}

# RSA/EC algorithms supported via the `cryptography` library
_ASYMMETRIC_ALGS: set[str] = {
    "RS256", "RS384", "RS512",
    "ES256", "ES384", "ES512",
    "PS256", "PS384", "PS512",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url segment, padding as needed."""
    segment = segment.replace("-", "+").replace("_", "/")
    padding = 4 - len(segment) % 4
    if padding != 4:
        segment += "=" * padding
    return base64.b64decode(segment)


def _decode_json_segment(segment: str, label: str) -> tuple[dict | None, str | None]:
    try:
        raw_bytes = _b64url_decode(segment)
    except Exception as exc:
        return None, f"base64url decode failed for {label}: {exc}"
    try:
        decoded = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        return None, f"JSON parse failed for {label}: {exc}"
    if not isinstance(decoded, dict):
        return None, f"{label} is not a JSON object"
    return decoded, None


def _utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _ts_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------

def _verify_hmac(
    signing_input: bytes,
    sig_bytes: bytes,
    secret: str,
    alg: str,
) -> tuple[bool, str | None]:
    digest_fn = _HMAC_ALGS.get(alg)
    if digest_fn is None:
        return False, f"Unsupported HMAC algorithm: {alg}"
    try:
        expected = hmac.new(secret.encode("utf-8"), signing_input, digest_fn).digest()
        return hmac.compare_digest(expected, sig_bytes), None
    except Exception as exc:
        return False, f"HMAC verification error: {exc}"


def _verify_asymmetric(
    signing_input: bytes,
    sig_bytes: bytes,
    public_key_pem: str,
    alg: str,
) -> tuple[bool, str | None]:
    """Verify RSA or EC signature using the `cryptography` library."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_public_key
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
        from cryptography.exceptions import InvalidSignature
    except ImportError:
        return False, (
            "'cryptography' package required for RSA/EC verification: pip install cryptography"
        )

    try:
        key = load_pem_public_key(public_key_pem.encode("utf-8"))
    except Exception as exc:
        return False, f"Failed to load public key: {exc}"

    _hash_map: dict[str, Any] = {
        "RS256": hashes.SHA256(), "RS384": hashes.SHA384(), "RS512": hashes.SHA512(),
        "PS256": hashes.SHA256(), "PS384": hashes.SHA384(), "PS512": hashes.SHA512(),
        "ES256": hashes.SHA256(), "ES384": hashes.SHA384(), "ES512": hashes.SHA512(),
    }
    hash_alg = _hash_map.get(alg)
    if hash_alg is None:
        return False, f"Unsupported asymmetric algorithm: {alg}"

    key_type = type(key).__name__
    try:
        if alg.startswith("RS"):
            key.verify(sig_bytes, signing_input, asym_padding.PKCS1v15(), hash_alg)  # type: ignore[union-attr]
        elif alg.startswith("PS"):
            key.verify(  # type: ignore[union-attr]
                sig_bytes, signing_input,
                asym_padding.PSS(mgf=asym_padding.MGF1(hash_alg), salt_length=asym_padding.PSS.MAX_LENGTH),
                hash_alg,
            )
        elif alg.startswith("ES"):
            key.verify(sig_bytes, signing_input, ECDSA(hash_alg))  # type: ignore[union-attr]
        else:
            return False, f"Unsupported asymmetric algorithm: {alg}"
        return True, None
    except InvalidSignature:
        return False, None
    except Exception as exc:
        return False, f"Signature verification error ({key_type}): {exc}"


# ---------------------------------------------------------------------------
# Claim analysis
# ---------------------------------------------------------------------------

def _human_delta(seconds: int, past: bool) -> str:
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    if past:
        return f"{days} day(s) ago" if days > 0 else f"{hours} hour(s) ago"
    return f"{days} day(s)" if days > 0 else f"{hours} hour(s)"


def _check_claims(
    payload: dict,
    now_ts: int,
) -> tuple[bool, bool, list[dict[str, str]]]:
    """Analyse standard time-bound claims. Returns (expired, not_yet_valid, issues)."""
    issues: list[dict[str, str]] = []
    expired = False
    not_yet_valid = False

    exp = payload.get("exp")
    if exp is not None:
        try:
            exp_int = int(exp)
            if exp_int < now_ts:
                issues.append({"field": "exp", "issue": f"Token expired {_human_delta(now_ts - exp_int, past=True)}"})
                expired = True
        except (TypeError, ValueError):
            issues.append({"field": "exp", "issue": f"exp claim is not a valid integer: {exp!r}"})

    nbf = payload.get("nbf")
    if nbf is not None:
        try:
            nbf_int = int(nbf)
            if nbf_int > now_ts:
                issues.append({"field": "nbf", "issue": f"Token not yet valid for {_human_delta(nbf_int - now_ts, past=False)}"})
                not_yet_valid = True
        except (TypeError, ValueError):
            issues.append({"field": "nbf", "issue": f"nbf claim is not a valid integer: {nbf!r}"})

    iat = payload.get("iat")
    if iat is not None:
        try:
            if int(iat) > now_ts:
                issues.append({"field": "iat", "issue": "iat (issued at) is in the future — possible clock skew or forged token"})
        except (TypeError, ValueError):
            issues.append({"field": "iat", "issue": f"iat claim is not a valid integer: {iat!r}"})

    return expired, not_yet_valid, issues


# ---------------------------------------------------------------------------
# Algorithm confusion risk detection
# ---------------------------------------------------------------------------

def _check_alg_confusion(alg: str, public_key: str | None) -> tuple[bool, str | None]:
    if alg.upper() == "NONE":
        return True, "alg:none attack — signature is never verified"
    if alg.upper() in _HMAC_ALGS and public_key is not None:
        return True, f"RS256→HS256 confusion risk: server may verify RSA public key as HMAC secret (alg={alg!r} but a public_key was provided)"
    return False, None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(payload: dict) -> dict:
    """Decode and security-analyse a JWT without a JWT library. Never raises."""
    if not isinstance(payload, dict):
        return _err("jwt_debugger.invalid_payload", "payload must be a JSON object")

    raw_token = payload.get("token")
    if not isinstance(raw_token, str) or not raw_token.strip():
        return _err(
            "jwt_debugger.missing_token",
            "'token' is required and must be a non-empty string",
        )

    token = raw_token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    secret: str | None = payload.get("secret")
    if secret is not None and not isinstance(secret, str):
        return _err("jwt_debugger.invalid_secret", "'secret' must be a string")
    secret = (secret or "").strip() or None

    public_key: str | None = payload.get("public_key")
    if public_key is not None and not isinstance(public_key, str):
        return _err("jwt_debugger.invalid_public_key", "'public_key' must be a string")
    public_key = (public_key or "").strip() or None

    parts = token.split(".")
    if len(parts) != 3:
        return _err("jwt_debugger.malformed", f"JWT must have exactly 3 dot-separated parts; got {len(parts)}")
    header_seg, payload_seg, sig_seg = parts

    header, header_err = _decode_json_segment(header_seg, "header")
    if header_err:
        return _err("jwt_debugger.malformed", header_err)

    jwt_payload, payload_err = _decode_json_segment(payload_seg, "payload")
    if payload_err:
        return _err("jwt_debugger.malformed", payload_err)

    try:
        sig_bytes = _b64url_decode(sig_seg)
    except Exception as exc:
        return _err("jwt_debugger.malformed", f"Signature base64url decode failed: {exc}")

    alg: str = str(header.get("alg", "")).upper()
    key_id: str | None = header.get("kid")
    now_ts = _utc_now_ts()
    signing_input = f"{header_seg}.{payload_seg}".encode("utf-8")

    signature_valid: bool | None = None
    verified = False
    sig_error: str | None = None

    if secret is not None:
        if alg in _HMAC_ALGS:
            signature_valid, sig_error = _verify_hmac(signing_input, sig_bytes, secret, alg)
            verified = bool(signature_valid)
        else:
            sig_error = f"Algorithm {alg!r} is not an HMAC algorithm; cannot verify with a plain secret"
    elif public_key is not None:
        if alg in _ASYMMETRIC_ALGS:
            signature_valid, sig_error = _verify_asymmetric(signing_input, sig_bytes, public_key, alg)
            if sig_error is None:
                verified = bool(signature_valid)
        else:
            sig_error = f"Algorithm {alg!r} is not a supported asymmetric algorithm; cannot verify with a public key"

    expired, not_yet_valid, claims_issues = _check_claims(jwt_payload, now_ts)
    if sig_error:
        claims_issues.append({"field": "signature", "issue": sig_error})

    alg_confusion_risk, alg_confusion_detail = _check_alg_confusion(alg, public_key)

    exp_val = jwt_payload.get("exp")
    exp_timestamp: int | None = None
    exp_human: str | None = None
    if exp_val is not None:
        try:
            exp_timestamp = int(exp_val)
            exp_human = _ts_to_iso(exp_timestamp)
        except (TypeError, ValueError):
            pass

    decoded_at = _ts_to_iso(now_ts)

    # Schema declares signature_valid as boolean. When no key was provided we
    # intentionally didn't verify; expose that distinction via `verified` and
    # collapse signature_valid → False so the contract validator stays happy.
    signature_valid_bool = bool(signature_valid) if signature_valid is not None else False

    return {
        "header": header,
        "payload": jwt_payload,
        "signature_valid": signature_valid_bool,
        "verified": verified,
        "algorithm": alg or None,
        "alg_confusion_risk": alg_confusion_risk,
        "alg_confusion_detail": alg_confusion_detail,
        "expired": expired,
        "not_yet_valid": not_yet_valid,
        "exp_timestamp": exp_timestamp,
        "exp_human": exp_human,
        "claims_issues": claims_issues,
        "key_id": key_id,
        "decoded_at": decoded_at,
    }
