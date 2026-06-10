"""Discover and replay a site's backing JSON API ("compile a site into an API").

# OWNS: the pure logic that turns captured XHR/fetch traffic into a replayable,
#        signable API-spec contract, and the no-browser replay of a stored spec.
# NOT OWNS: signing (signing.py), persistence (store.py), the browser render
#           (the navigator owns the page; we only attach a read-only listener).
# INVARIANTS:
#   * The endpoint AUTHORITY (scheme/host/port) is the signed, non-templatable
#     contract. Params may substitute ONLY into path/query, are percent-encoded,
#     and reconstruct_endpoint re-parses to prove the authority never changed.
#   * Replay validates the URL (SSRF) AND pins the resolved IP for the connect
#     (DNS-rebind defense, shared with core.outbound_session). follow_redirects
#     is False — the signed host is the contract, a redirect is a miss.
#   * Reuse across authors additionally requires the endpoint host to share the
#     page's registrable domain (same_registrable_domain) — enforced by the
#     authoring/replay callers, not here, but the helper lives here.
# DECISIONS:
#   * v1 emits LITERAL specs (no {param} placeholders). reconstruct_endpoint still
#     handles + sanitises params so the templating path is security-tested and
#     ready; the authoring side simply doesn't produce templates yet.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from core import outbound_session, url_security
from core.web import fetch_backend as _fetch_backend

_LOG = logging.getLogger(__name__)

# Capture bounds — a hostile page can emit unbounded/huge XHRs; cap everything.
_DISCOVERY_MAX_CAPTURES = 40
_DISCOVERY_MAX_BODY_BYTES = 512_000
_DISCOVERY_JSON_CTYPES = ("application/json", "application/ld+json", "text/json")
_XHR_RESOURCE_TYPES = ("xhr", "fetch")

_REPLAY_TIMEOUT_S = 10.0
_REPLAY_UA = "Aztea-Site-Navigator/1.0 (api-replay)"

# {param} placeholders may appear only in path/query templates (v1 emits none).
_PARAM_RE = re.compile(r"\{([a-zA-Z0-9_]+)\}")

# Registrable-domain heuristic without a full public-suffix list: the common
# multi-label public suffixes whose eTLD+1 is the last THREE labels. Conservative
# — anything not listed falls back to last-two-labels, which is the safe default
# for a same-origin-ish reuse gate (it can only ever be stricter, never looser).
_MULTI_LABEL_TLDS = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk",
    "co.jp", "or.jp", "ne.jp", "go.jp", "ac.jp",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.nz", "org.nz", "govt.nz", "ac.nz",
    "com.br", "net.br", "org.br", "gov.br",
    "co.in", "net.in", "org.in", "gov.in", "ac.in",
    "co.za", "org.za", "gov.za", "ac.za",
    "com.sg", "com.hk", "com.cn", "com.mx", "com.tr", "co.kr",
})


# --------------------------------------------------------------------------- domain provenance
def registrable_domain(host: str) -> str:
    """Pure: best-effort eTLD+1 of ``host`` (the same-origin-ish reuse anchor).

    IP literals return themselves. Falls back to the last two labels when the
    suffix isn't a known multi-label TLD — a stricter-not-looser approximation,
    which is the safe direction for a security gate.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return ""
    try:
        ipaddress.ip_address(h)
        return h  # IP literal is its own registrable identity
    except ValueError:
        pass
    labels = h.split(".")
    if len(labels) <= 2:
        return h
    if ".".join(labels[-2:]) in _MULTI_LABEL_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_registrable_domain(host_a: str, host_b: str) -> bool:
    """Pure: True iff both hosts share a non-empty registrable domain.

    The fix for cross-origin spec poisoning: a discovered endpoint may only be
    reused for a page when it lives on the same registrable domain, so an author
    cannot register ``site_key=bank.com`` pointing at ``attacker.com``.
    """
    rd = registrable_domain(host_a)
    return bool(rd) and rd == registrable_domain(host_b)


# --------------------------------------------------------------------------- endpoint split / rebuild
@dataclasses.dataclass(frozen=True)
class EndpointParts:
    """The SIGNED authority (scheme/host/port) vs the TEMPLATABLE path/query."""

    scheme: str
    host: str
    port: int | None
    path: str
    query: str


def split_endpoint(url: str) -> EndpointParts:
    """Pure: split an absolute http(s) URL into signed-authority vs templatable parts."""
    parsed = urlparse(str(url or ""))
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        raise ValueError("split_endpoint requires an absolute http(s) URL with a host")
    return EndpointParts(
        scheme=scheme, host=host, port=parsed.port,
        path=parsed.path or "/", query=parsed.query or "",
    )


def _substitute(template: str, params: dict[str, Any]) -> str:
    """Pure: replace {key} with percent-encoded params[key]. Missing param -> error."""
    def _repl(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in params:
            raise ValueError(f"api_spec template references unknown param: {key}")
        return quote(str(params[key]), safe="")  # safe="" so '/', '@', '?', ':' are all encoded
    return _PARAM_RE.sub(_repl, template)


def reconstruct_endpoint(spec: dict[str, Any], params: dict[str, Any] | None = None) -> str:
    """Pure: rebuild the absolute endpoint URL from the SIGNED authority + path/query.

    The authority comes only from the signed columns; params substitute, fully
    percent-encoded, ONLY into the path/query templates. After building, the URL is
    re-parsed and we assert the host + scheme are unchanged — so no param value can
    smuggle a new authority in (the ``@evil.com`` / ``evil.com/`` injection).
    """
    scheme = str(spec.get("endpoint_scheme") or "").lower()
    host = str(spec.get("endpoint_host") or "").lower()
    if scheme not in ("http", "https") or not host:
        raise ValueError("api_spec has no valid signed authority")
    port = spec.get("endpoint_port")
    params = params or {}
    path = _substitute(str(spec.get("path_template") or "/"), params)
    query = _substitute(str(spec.get("query_template") or ""), params)
    if not path.startswith("/"):
        path = "/" + path
    authority = host if port in (None, "") else f"{host}:{int(port)}"
    url = f"{scheme}://{authority}{path}"
    if query:
        url = f"{url}?{query}"
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != host or parsed.scheme != scheme:
        raise ValueError("api_spec param substitution attempted to alter the endpoint authority")
    return url


# --------------------------------------------------------------------------- candidate selection
def _tokens(text: str) -> set[str]:
    """Pure: lowercase alphanumeric tokens of length >= 3."""
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(t) >= 3}


def _is_nontrivial_json(body: Any) -> bool:
    """Pure: a dict with >=1 key or a non-empty list — worth compiling into a spec."""
    if isinstance(body, dict):
        return len(body) >= 1
    if isinstance(body, list):
        return len(body) >= 1
    return False


def _candidate_score(capture: dict[str, Any], goal_tokens: set[str]) -> float:
    """Pure: goal-token overlap in the URL path, with a small richness tie-break."""
    url_tokens = _tokens(urlparse(str(capture.get("url") or "")).path)
    overlap = len(goal_tokens & url_tokens)
    body = capture.get("json")
    richness = len(body) if isinstance(body, (dict, list)) else 0
    return overlap * 10.0 + min(richness, 20) * 0.1


def select_candidate(
    captures: list[dict[str, Any]], *, goal: str, rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Pure: pick the best JSON XHR capture to compile into a spec, or None.

    v1 heuristic: GET + non-trivial JSON body wins; among those, prefer goal-token
    overlap in the URL path, then a richer shape. POSTs and trivial bodies are
    skipped — a literal POST replay is low-value and higher-risk in v1.
    """
    goal_tokens = _tokens(goal)
    best: dict[str, Any] | None = None
    best_score = -1.0
    for cap in captures:
        if str(cap.get("method") or "GET").upper() != "GET":
            continue
        if not _is_nontrivial_json(cap.get("json")):
            continue
        score = _candidate_score(cap, goal_tokens)
        if score > best_score:
            best, best_score = cap, score
    return best


# --------------------------------------------------------------------------- capture (Playwright)
def attach_xhr_capture(page: Any, sink: list[dict[str, Any]]) -> None:
    """Side-effect: register a requestfinished listener that buffers JSON XHR/fetch
    responses into ``sink``.

    Bounded by _DISCOVERY_MAX_CAPTURES / _DISCOVERY_MAX_BODY_BYTES and gated to JSON
    content-types. Non-JSON, oversized, or parse-failing responses are dropped
    (debug-logged). The listener NEVER raises into the page — a capture failure must
    never break the navigation it is observing.
    """
    def _on_finished(request: Any) -> None:
        if len(sink) >= _DISCOVERY_MAX_CAPTURES:
            return
        try:
            if getattr(request, "resource_type", None) not in _XHR_RESOURCE_TYPES:
                return
            response = request.response()
            if response is None:
                return
            ctype = str((response.headers or {}).get("content-type", "")).lower()
            if not any(j in ctype for j in _DISCOVERY_JSON_CTYPES):
                return
            body = response.body()
            if not body or len(body) > _DISCOVERY_MAX_BODY_BYTES:
                return
            sink.append({
                "method": str(getattr(request, "method", "GET")).upper(),
                "url": getattr(request, "url", ""),
                "post_data": getattr(request, "post_data", None),
                "status": getattr(response, "status", None),
                "content_type": ctype,
                "json": json.loads(body.decode("utf-8", "replace")),
            })
        except Exception:  # noqa: BLE001 — capture is best-effort; never break the nav
            _LOG.debug("xhr capture skipped", exc_info=True)

    page.on("requestfinished", _on_finished)


# --------------------------------------------------------------------------- replay (no browser)
def replay(url: str, *, method: str = "GET", post_data: Any = None) -> Any | None:
    """Side-effect: direct HTTP replay of a discovered endpoint — no browser.

    SSRF: ``validate_outbound_url`` first, then pin the resolved IP for the connect
    (DNS-rebind defense, shared with core.outbound_session). Returns the parsed JSON
    body (dict or list), or None on any failure — replay is an optimisation, so a
    miss falls back to a full render. ``follow_redirects=False``: the signed host is
    the contract, a redirect is treated as a miss.
    """
    try:
        url_security.validate_outbound_url(url, "api_spec_endpoint")
    except ValueError:
        _LOG.info("api_spec replay blocked by SSRF policy: %s", url)
        return None
    try:
        with outbound_session.pinned_ip_for_url(url):
            with httpx.Client(
                follow_redirects=False, timeout=_REPLAY_TIMEOUT_S,
                headers={"User-Agent": _REPLAY_UA}, **_fetch_backend.httpx_kwargs(),
            ) as client:
                resp = (
                    client.post(url, content=post_data)
                    if method.upper() == "POST"
                    else client.get(url)
                )
        if resp.status_code >= 400 or len(resp.content) > _DISCOVERY_MAX_BODY_BYTES:
            return None
        return resp.json()
    except Exception:  # noqa: BLE001 — replay is an optimisation; fall back to render
        _LOG.debug("api_spec replay failed for %s", url, exc_info=True)
        return None
