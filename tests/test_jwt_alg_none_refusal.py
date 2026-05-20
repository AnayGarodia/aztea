"""C-1 (audit 2026-05-19): jwt_validator must refuse tokens whose header
declares ``alg: none`` regardless of the caller-supplied algorithms list.

Pre-fix, a caller passing ``algorithms=["HS256"]`` with a token header of
``alg=none`` got back ``signature_valid: null, verified_with: "none",
errors: []`` — downstream code reading ``errors == []`` could conclude the
token was fine. Now: structured 422 ``jwt_validator.alg_none_refused``
fires before any verification path even when the allowlist excludes none.
"""
from __future__ import annotations

import base64
import json

from agents.jwt_validator import run as jwt_run


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _alg_none_token() -> str:
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url({"sub": "admin", "admin": True})
    return f"{header}.{payload}."


def test_alg_none_token_refused_default_algorithms():
    """Caller doesn't pass an algorithms list — refusal still fires."""
    result = jwt_run({"token": _alg_none_token()})
    assert "error" in result
    assert result["error"]["code"] == "jwt_validator.alg_none_refused"


def test_alg_none_token_refused_with_strict_algorithms_list():
    """Caller explicitly excludes 'none' — refusal still fires because the
    REJECTION is now in the header parse, not in the caller-supplied list."""
    result = jwt_run({
        "token": _alg_none_token(),
        "algorithms": ["HS256", "RS256"],
    })
    assert "error" in result
    assert result["error"]["code"] == "jwt_validator.alg_none_refused"


def test_alg_none_in_caller_algorithms_still_refused_at_resolution():
    """Existing behavior at _resolve_algorithms still rejects 'none' in the
    allowlist itself — independent gate, kept for defense-in-depth."""
    result = jwt_run({
        "token": _alg_none_token(),
        "algorithms": ["none"],
    })
    assert "error" in result
    # Either gate may fire first — both produce a structured refusal.
    assert result["error"]["code"] in {
        "jwt_validator.alg_none_refused",
        "jwt_validator.invalid_algorithms",
    }


def test_non_none_header_still_decodes():
    """Smoke test: a token with alg=HS256 in the header decodes normally."""
    header = _b64url({"alg": "HS256", "typ": "JWT"})
    payload = _b64url({"sub": "user-1"})
    token = f"{header}.{payload}.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    result = jwt_run({"token": token})
    assert "error" not in result
    assert result["header"] == {"alg": "HS256", "typ": "JWT"}
    assert result["payload"] == {"sub": "user-1"}
