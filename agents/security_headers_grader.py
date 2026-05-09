"""
security_headers_grader.py — Grade HTTP security headers like securityheaders.com.

Input:
  {
    "url": "https://example.com",   # required
    "follow_redirects": true          # default true
  }

Output:
  {
    "url": str,
    "final_url": str,
    "status_code": int,
    "redirect_chain": [{"url": str, "status_code": int}],
    "grade": "A+|A|B|C|D|F",
    "score": int,                     # 0-100
    "headers": {                      # observed values (raw, truncated)
      "strict_transport_security": str | null,
      "content_security_policy": str | null,
      "x_frame_options": str | null,
      "x_content_type_options": str | null,
      "referrer_policy": str | null,
      "permissions_policy": str | null,
      "cross_origin_opener_policy": str | null,
      "cross_origin_embedder_policy": str | null,
      "cross_origin_resource_policy": str | null
    },
    "missing": [str],                 # list of important headers absent
    "weak": [{"header": str, "issue": str}],
    "passed": [str],
    "tls": {
      "is_https": bool
    },
    "billing_units_actual": int       # always 1
  }
"""

from __future__ import annotations

from typing import Any

import httpx

from core.url_security import validate_outbound_url
from agents._contracts import agent_error as _err

_TIMEOUT_S = 10.0
_MAX_REDIRECTS = 5
_HEADER_VALUE_TRUNCATE = 400
_USER_AGENT = "Aztea-Security-Headers-Grader/1.0"
_NO_HTTPS_SCORE_CEILING = 30
_WEAK_HEADER_POINT_FRACTION = 0.5
_SCORE_MIN, _SCORE_MAX = 0, 100

# Lower-case canonical mapping. Each header carries the points it's worth and
# an optional weakness check (returns a string when the present value is sub-par).
_HEADER_CHECKS: dict[str, dict[str, Any]] = {
    "strict-transport-security": {
        "out_key": "strict_transport_security",
        "points": 20,
        "required": True,
        "weak": lambda v: (
            "max-age too short (< 6 months); add max-age=15552000+"
            if "max-age" in v.lower()
            and _hsts_max_age(v) is not None
            and (_hsts_max_age(v) or 0) < 15_552_000
            else (
                "missing 'includeSubDomains'"
                if "includesubdomains" not in v.lower()
                else None
            )
        ),
    },
    "content-security-policy": {
        "out_key": "content_security_policy",
        "points": 25,
        "required": True,
        "weak": lambda v: (
            "uses 'unsafe-inline' (script execution loophole)"
            if "'unsafe-inline'" in v.lower()
            else (
                "uses 'unsafe-eval'"
                if "'unsafe-eval'" in v.lower()
                else None
            )
        ),
    },
    "x-frame-options": {
        "out_key": "x_frame_options",
        "points": 10,
        "required": True,
        "weak": lambda v: (
            None if v.strip().upper() in {"DENY", "SAMEORIGIN"} else "value should be DENY or SAMEORIGIN"
        ),
    },
    "x-content-type-options": {
        "out_key": "x_content_type_options",
        "points": 10,
        "required": True,
        "weak": lambda v: (None if v.strip().lower() == "nosniff" else "value should be 'nosniff'"),
    },
    "referrer-policy": {
        "out_key": "referrer_policy",
        "points": 10,
        "required": True,
        "weak": lambda v: (
            None
            if v.strip().lower()
            in {
                "no-referrer",
                "no-referrer-when-downgrade",
                "strict-origin",
                "strict-origin-when-cross-origin",
                "same-origin",
            }
            else "consider 'strict-origin-when-cross-origin'"
        ),
    },
    "permissions-policy": {
        "out_key": "permissions_policy",
        "points": 10,
        "required": True,
        "weak": lambda v: None,
    },
    "cross-origin-opener-policy": {
        "out_key": "cross_origin_opener_policy",
        "points": 5,
        "required": False,
        "weak": lambda v: (
            None
            if v.strip().lower() in {"same-origin", "same-origin-allow-popups"}
            else "consider 'same-origin'"
        ),
    },
    "cross-origin-embedder-policy": {
        "out_key": "cross_origin_embedder_policy",
        "points": 5,
        "required": False,
        "weak": lambda v: None,
    },
    "cross-origin-resource-policy": {
        "out_key": "cross_origin_resource_policy",
        "points": 5,
        "required": False,
        "weak": lambda v: None,
    },
}

# Headers that leak server info — present is bad.
_LEAKY_HEADERS = {"server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version"}
_LEAKY_PENALTY_PER = 3
_MAX_LEAKY_PENALTY = 10


def _hsts_max_age(value: str) -> int | None:
    for token in value.split(";"):
        token = token.strip().lower()
        if token.startswith("max-age"):
            try:
                return int(token.split("=", 1)[1].strip())
            except (IndexError, ValueError):
                return None
    return None



def _grade_from_score(score: int, is_https: bool, has_csp: bool, has_hsts: bool) -> str:
    if not is_https:
        return "F"
    if score >= 95 and has_csp and has_hsts:
        return "A+"
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _truncate(value: str) -> str:
    return value if len(value) <= _HEADER_VALUE_TRUNCATE else value[:_HEADER_VALUE_TRUNCATE] + "…"


def _follow_redirect_chain(
    client: httpx.Client, url: str, *, follow_redirects: bool,
) -> dict | tuple[Any, list[dict[str, Any]]]:
    """Side-effect: walk redirects with SSRF-checked Location headers; returns ``(response, chain)`` or error envelope."""
    redirect_chain: list[dict[str, Any]] = []
    current_url = url
    response = None
    for _ in range(_MAX_REDIRECTS + 1):
        response = client.get(current_url)
        redirect_chain.append({"url": current_url, "status_code": int(response.status_code)})
        if not (follow_redirects and 300 <= response.status_code < 400):
            return response, redirect_chain
        location = response.headers.get("location")
        if not location:
            return response, redirect_chain
        next_url = str(httpx.URL(current_url).join(location))
        try:
            next_url = validate_outbound_url(next_url, "redirect")
        except ValueError as exc:
            return _err(
                "security_headers_grader.redirect_blocked",
                f"Redirect target rejected by SSRF policy: {exc}",
            )
        current_url = next_url
    return _err(
        "security_headers_grader.too_many_redirects",
        f"Hit redirect cap of {_MAX_REDIRECTS}.",
    )


def _grade_one_header(
    headers_lc: dict[str, str], header_name: str, meta: dict[str, Any],
) -> tuple[str | None, int, str | None, str | None]:
    """Pure: ``(out_value, points, missing_name_or_None, weak_issue_or_None)`` for one header."""
    present = headers_lc.get(header_name)
    if present is None:
        return None, 0, (header_name if meta["required"] else None), None
    value = present.strip()
    out_value = _truncate(value)
    weakness = meta["weak"](value) if callable(meta["weak"]) else None
    if weakness:
        return out_value, int(meta["points"] * _WEAK_HEADER_POINT_FRACTION), None, weakness
    return out_value, int(meta["points"]), None, None


def _grade_headers(
    headers_lc: dict[str, str],
) -> tuple[dict[str, str | None], int, list[str], list[str], list[dict[str, str]]]:
    """Pure: walk ``_HEADER_CHECKS``; returns ``(out_headers, raw_score, passed, missing, weak)``."""
    out_headers: dict[str, str | None] = {
        meta["out_key"]: None for meta in _HEADER_CHECKS.values()
    }
    raw_score = 0
    passed: list[str] = []
    missing: list[str] = []
    weak: list[dict[str, str]] = []
    for header_name, meta in _HEADER_CHECKS.items():
        out_value, points, miss, weak_issue = _grade_one_header(headers_lc, header_name, meta)
        if out_value is not None:
            out_headers[meta["out_key"]] = out_value
        raw_score += points
        if miss:
            missing.append(miss)
        if weak_issue:
            weak.append({"header": header_name, "issue": weak_issue})
        elif out_value is not None:
            passed.append(header_name)
    return out_headers, raw_score, passed, missing, weak


def _apply_score_penalties(
    score: int, *, is_https: bool, headers_lc: dict[str, str],
) -> tuple[int, list[str]]:
    """Pure: apply HTTPS-floor and leaky-header penalties; returns ``(score, leaky_headers)``."""
    if not is_https:
        score = min(score, _NO_HTTPS_SCORE_CEILING)
    leaky_found = [h for h in _LEAKY_HEADERS if h in headers_lc]
    leaky_penalty = min(len(leaky_found) * _LEAKY_PENALTY_PER, _MAX_LEAKY_PENALTY)
    return max(_SCORE_MIN, min(_SCORE_MAX, score - leaky_penalty)), leaky_found


def _normalize_run_inputs(payload: dict) -> dict | tuple[str, bool]:
    """Pure: validate ``url`` and ``follow_redirects``; returns parsed bag or error envelope."""
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("security_headers_grader.missing_url", "url is required")
    try:
        url = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("security_headers_grader.invalid_url", str(exc))
    return url, bool(payload.get("follow_redirects", True))


def _fetch_with_redirects(
    url: str, follow_redirects: bool,
) -> dict | tuple[Any, list[dict[str, Any]]]:
    """Side-effect: open the client and walk the redirect chain; returns ``(response, chain)`` or error envelope."""
    try:
        with httpx.Client(
            timeout=_TIMEOUT_S, follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        ) as client:
            return _follow_redirect_chain(client, url, follow_redirects=follow_redirects)
    except httpx.HTTPError as exc:
        return _err(
            "security_headers_grader.fetch_failed",
            f"HTTP fetch failed: {type(exc).__name__}: {exc}",
        )


def _build_grade_response(
    *, url: str, response: Any, redirect_chain: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pure: shape the final response from a successful fetch."""
    final_url = redirect_chain[-1]["url"]
    is_https = final_url.lower().startswith("https://")
    headers_lc = {k.lower(): v for k, v in response.headers.items()}
    out_headers, raw_score, passed, missing, weak = _grade_headers(headers_lc)
    score, leaky_found = _apply_score_penalties(
        raw_score, is_https=is_https, headers_lc=headers_lc,
    )
    grade = _grade_from_score(
        score,
        is_https=is_https,
        has_csp=out_headers["content_security_policy"] is not None,
        has_hsts=out_headers["strict_transport_security"] is not None,
    )
    return {
        "url": url,
        "final_url": final_url,
        "status_code": int(response.status_code),
        "redirect_chain": redirect_chain,
        "grade": grade,
        "score": score,
        "headers": out_headers,
        "missing": sorted(missing),
        "weak": weak,
        "passed": sorted(passed),
        "leaky_headers": sorted(leaky_found),
        "tls": {"is_https": is_https},
        "billing_units_actual": 1,
    }


def run(payload: dict) -> dict:
    """Fetch a URL and grade its HTTP security headers (CSP, HSTS, X-Frame-Options, etc).

    Why: pure HTTP — no browser, no LLM. Score is the sum of per-header
    weights with HTTPS as a hard ceiling and a leaky-header penalty so
    sites that ship Server/X-Powered-By can't max out.
    """
    parsed = _normalize_run_inputs(payload)
    if isinstance(parsed, dict):
        return parsed
    url, follow_redirects = parsed
    outcome = _fetch_with_redirects(url, follow_redirects)
    if isinstance(outcome, dict):
        return outcome  # error envelope
    response, redirect_chain = outcome
    return _build_grade_response(url=url, response=response, redirect_chain=redirect_chain)
