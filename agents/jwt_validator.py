"""
jwt_validator.py — Decode and verify a JWT.

Input:
  {
    "token": "eyJhbGciOi...",            # required
    "secret": "supersecret",             # optional, HMAC verification
    "jwks_url": "https://.../jwks.json", # optional, RSA/EC public-key
    "algorithms": ["HS256", "RS256"]     # optional allow-list
  }

Output:
  {
    "header": dict | null,
    "payload": dict | null,
    "signature_valid": bool | null,    # null when no key/secret supplied
    "exp_valid": bool | null,           # null when no exp claim present
    "nbf_valid": bool | null,
    "iat_valid": bool | null,
    "verified_with": "secret" | "jwks" | "none",
    "errors": [str]
  }

OWNS: decode + optional verification; multiple alg allow-listing.
NOT OWNS: key rotation, token issuance, refresh-token semantics.
INVARIANTS:
  * Never silently accept an unverified token — ``signature_valid`` is
    null only when no verification material was provided.
  * Never accept "none" as a verification algorithm even if listed.
"""

from __future__ import annotations

import base64
import json
import logging
import time

import requests

from agents._contracts import agent_error as _err
from core import url_security

_LOG = logging.getLogger(__name__)

_JWT_PARTS = 3
_MAX_TOKEN_CHARS = 10_000
_JWKS_FETCH_TIMEOUT_S = 5.0
_USER_AGENT = "Aztea-JWT-Validator/1.0"
_DEFAULT_ALGORITHMS = ("HS256", "HS384", "HS512", "RS256", "RS384", "RS512", "ES256", "ES384")


def _b64url_decode(s: str) -> bytes:
    """Pure: base64url-decode a JWT segment, padding as needed."""
    pad = -len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def _decode_segment(segment: str) -> dict | None:
    """Pure: decode one JWT segment to JSON; ``None`` on malformed input."""
    try:
        raw = _b64url_decode(segment)
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — corrupt JWTs are user errors, not exceptions
        return None


def _check_time_claims(payload: dict) -> dict[str, bool | None]:
    """Pure: evaluate exp / nbf / iat against the current epoch.

    Each result is ``None`` when the corresponding claim is absent — that
    distinction matters for callers (a missing exp is not the same as an
    expired token).
    """
    now = int(time.time())
    out: dict[str, bool | None] = {
        "exp_valid": None,
        "nbf_valid": None,
        "iat_valid": None,
    }
    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        out["exp_valid"] = now < int(exp)
    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)):
        out["nbf_valid"] = now >= int(nbf)
    iat = payload.get("iat")
    if isinstance(iat, (int, float)):
        # iat in the future is suspicious; allow 60s skew.
        out["iat_valid"] = int(iat) <= now + 60
    return out


def _resolve_algorithms(payload: dict) -> list[str]:
    """Pure: collect caller-permitted algorithms, defaulting to the safe set.

    "none" is rejected to prevent the canonical JWT pitfall — an attacker
    forging ``{"alg":"none"}`` and a server "verifying" it with no key.
    """
    raw = payload.get("algorithms")
    if raw is None:
        return list(_DEFAULT_ALGORITHMS)
    if not isinstance(raw, list):
        raise ValueError("algorithms must be a list of strings")
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip().upper()
        if not s:
            continue
        if s == "NONE":
            raise ValueError("algorithm 'none' is not allowed for verification")
        out.append(s)
    return out or list(_DEFAULT_ALGORITHMS)


def _verify_with_pyjwt(token: str, secret: str, algorithms: list[str]) -> tuple[bool, str | None]:
    """Side-effect: verify a JWT signature using PyJWT.

    Returns ``(verified, error_message)``. Lazy-imports PyJWT — agents
    must remain importable on workers without the dependency.
    """
    try:
        import jwt  # type: ignore[import]
    except ImportError:
        return False, "PyJWT not installed; signature unverified"
    try:
        jwt.decode(token, secret, algorithms=algorithms)
        return True, None
    except jwt.PyJWTError as exc:
        return False, f"PyJWT verification failed: {exc}"
    except Exception as exc:  # noqa: BLE001 — defensive: never propagate
        return False, f"verification raised: {type(exc).__name__}"


def _fetch_jwks(jwks_url: str) -> dict | None:
    """Side-effect: fetch a JWKS document; ``None`` on any error.

    Why: SSRF-validated and bounded — the agent is callable for public
    OIDC providers but cannot pivot to internal endpoints.
    """
    try:
        url_security.validate_outbound_url(jwks_url, "jwks_url")
    except Exception as exc:
        _LOG.info("jwks_url rejected by SSRF gate: %s", exc)
        return None
    try:
        resp = requests.get(
            jwks_url, timeout=_JWKS_FETCH_TIMEOUT_S,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001
        _LOG.warning("JWKS fetch failed for %s", jwks_url, exc_info=True)
        return None


def _verify_with_jwks(token: str, jwks_url: str, algorithms: list[str]) -> tuple[bool, str | None]:
    """Side-effect: verify a JWT using a JWKS endpoint via PyJWT."""
    try:
        import jwt  # type: ignore[import]
        from jwt import PyJWKClient  # type: ignore[import]
    except ImportError:
        return False, "PyJWT not installed; signature unverified"
    try:
        url_security.validate_outbound_url(jwks_url, "jwks_url")
    except Exception as exc:
        return False, f"jwks_url blocked: {exc}"
    try:
        client = PyJWKClient(jwks_url)
        signing_key = client.get_signing_key_from_jwt(token).key
        jwt.decode(token, signing_key, algorithms=algorithms)
        return True, None
    except Exception as exc:  # noqa: BLE001 — multiple PyJWT exception types
        return False, f"JWKS verification failed: {type(exc).__name__}: {exc}"


def run(payload: dict) -> dict:
    """Decode a JWT, optionally verifying its signature and time claims.

    Why: callers routinely paste tokens to inspect their claims — a
    structured decode + signature check is faster and safer than wiring
    PyJWT into a one-off script.
    """
    if not isinstance(payload, dict):
        return _err("jwt_validator.bad_input",
                    f"payload must be dict, got {type(payload).__name__}")
    token = str(payload.get("token") or "").strip()
    if not token:
        return _err("jwt_validator.missing_token", "'token' is required.")
    if len(token) > _MAX_TOKEN_CHARS:
        return _err(
            "jwt_validator.token_too_long",
            f"token exceeds {_MAX_TOKEN_CHARS} chars",
        )
    parts = token.split(".")
    if len(parts) != _JWT_PARTS:
        return _err(
            "jwt_validator.malformed_token",
            f"expected 3 dot-separated segments, got {len(parts)}",
        )
    try:
        algorithms = _resolve_algorithms(payload)
    except ValueError as exc:
        return _err("jwt_validator.invalid_algorithms", str(exc))
    header = _decode_segment(parts[0])
    body = _decode_segment(parts[1])
    # C-1 (audit 2026-05-19): defense-in-depth — refuse any token whose
    # header declares alg=none, regardless of what the caller put in the
    # ``algorithms`` allowlist. Pre-fix, a caller passing
    # algorithms=["HS256"] with a token header of alg=none got back
    # ``signature_valid: null, verified_with: "none", errors: []``, which
    # downstream code reading ``errors == []`` may treat as "no problem"
    # and accept admin claims from an unsigned JWT. Now: structured 422.
    if isinstance(header, dict):
        _header_alg = str(header.get("alg") or "").strip().lower()
        if _header_alg == "none":
            return _err(
                "jwt_validator.alg_none_refused",
                "token header declares alg=none; refused regardless of the "
                "caller-supplied algorithms allowlist (CVE-2015-9235 class).",
            )
    errors: list[str] = []
    if header is None:
        errors.append("header segment is not valid base64url JSON")
    if body is None:
        errors.append("payload segment is not valid base64url JSON")
    time_claims = _check_time_claims(body or {})
    secret = payload.get("secret")
    jwks_url = payload.get("jwks_url")
    signature_valid: bool | None = None
    verified_with = "none"
    if isinstance(secret, str) and secret:
        ok, err = _verify_with_pyjwt(token, secret, algorithms)
        signature_valid = ok
        verified_with = "secret"
        if err:
            errors.append(err)
    elif isinstance(jwks_url, str) and jwks_url.strip():
        ok, err = _verify_with_jwks(token, jwks_url.strip(), algorithms)
        signature_valid = ok
        verified_with = "jwks"
        if err:
            errors.append(err)
    return {
        "header": header,
        "payload": body,
        "signature_valid": signature_valid,
        "verified_with": verified_with,
        "algorithms_allowed": algorithms,
        **time_claims,
        "errors": errors,
    }
