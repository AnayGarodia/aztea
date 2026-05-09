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


def run(payload: dict) -> dict:
    """Fetch a URL and grade its HTTP security headers (CSP, HSTS, X-Frame-Options, etc).

    Returns a numeric score (0-100), letter grade, per-header observations,
    a list of missing headers, weak-value flags, and the redirect chain that
    led to the final response. Pure HTTP — no LLM, no browser.
    """
    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        return _err("security_headers_grader.missing_url", "url is required")

    try:
        url = validate_outbound_url(raw_url, "url")
    except ValueError as exc:
        return _err("security_headers_grader.invalid_url", str(exc))

    follow_redirects = bool(payload.get("follow_redirects", True))

    redirect_chain: list[dict[str, Any]] = []
    try:
        with httpx.Client(
            timeout=_TIMEOUT_S,
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        ) as client:
            current_url = url
            response = None
            for _ in range(_MAX_REDIRECTS + 1):
                response = client.get(current_url)
                redirect_chain.append(
                    {"url": current_url, "status_code": int(response.status_code)}
                )
                if not (follow_redirects and 300 <= response.status_code < 400):
                    break
                location = response.headers.get("location")
                if not location:
                    break
                # Resolve relative redirects against the current URL and re-validate
                next_url = str(httpx.URL(current_url).join(location))
                try:
                    next_url = validate_outbound_url(next_url, "redirect")
                except ValueError as exc:
                    return _err(
                        "security_headers_grader.redirect_blocked",
                        f"Redirect target rejected by SSRF policy: {exc}",
                    )
                current_url = next_url
            else:
                return _err(
                    "security_headers_grader.too_many_redirects",
                    f"Hit redirect cap of {_MAX_REDIRECTS}.",
                )
    except httpx.HTTPError as exc:
        return _err(
            "security_headers_grader.fetch_failed",
            f"HTTP fetch failed: {type(exc).__name__}: {exc}",
        )

    assert response is not None
    final_url = redirect_chain[-1]["url"]
    is_https = final_url.lower().startswith("https://")

    # Lower-case all header names for lookup; keep original values.
    headers_lc = {k.lower(): v for k, v in response.headers.items()}

    out_headers: dict[str, str | None] = {meta["out_key"]: None for meta in _HEADER_CHECKS.values()}
    score = 0
    passed: list[str] = []
    missing: list[str] = []
    weak: list[dict[str, str]] = []

    for header_name, meta in _HEADER_CHECKS.items():
        present = headers_lc.get(header_name)
        if present is None:
            if meta["required"]:
                missing.append(header_name)
            continue

        value = present.strip()
        out_headers[meta["out_key"]] = _truncate(value)

        weakness = meta["weak"](value) if callable(meta["weak"]) else None
        if weakness:
            weak.append({"header": header_name, "issue": weakness})
            score += int(meta["points"] * 0.5)
        else:
            passed.append(header_name)
            score += int(meta["points"])

    # HTTPS itself is the price of admission — no HTTPS, score floors hard.
    if not is_https:
        score = min(score, 30)

    # Penalize leaky headers (Server, X-Powered-By, etc).
    leaky_found = [h for h in _LEAKY_HEADERS if h in headers_lc]
    leaky_penalty = min(len(leaky_found) * _LEAKY_PENALTY_PER, _MAX_LEAKY_PENALTY)
    score = max(0, min(100, score - leaky_penalty))

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
