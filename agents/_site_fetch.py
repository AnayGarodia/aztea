"""HTTP-first fetch for site_navigator: try a plain GET before launching Chromium.

# OWNS: the render-strategy enum and the SSRF-safe static fetch (per-hop
#        re-validation + IP-pinning + byte/redirect caps).
# NOT OWNS: HTML->rows / markdown / the needs-browser heuristic (agents/_html_extract.py),
#           the LLM resolve, the commons. Agent-private (underscore) like _contracts.py.
# INVARIANTS:
#   * Every fetched URL AND every redirect hop passes validate_outbound_url and has
#     its resolved IP pinned for the connect (DNS-rebind defense). validate_outbound_url
#     does not follow redirects, so follow_redirects=False + a manual hop loop is the
#     only correct pattern here.
# DECISIONS:
#   * Split from _html_extract so neither file approaches the 1000-line budget and the
#     network concern stays separate from the parsing concern (review fix #9).
"""

from __future__ import annotations

import dataclasses
import enum
import logging
from urllib.parse import urljoin

import httpx

from core import outbound_session, url_security
from core.web import fetch_backend as _fetch_backend

_LOG = logging.getLogger(__name__)

# RenderStrategy.CHROMIUM.value MUST equal the navigator's existing modality string
# so the Chromium path's output is unchanged when the new branches are off.
_MODALITY_AX = "accessibility_tree"


class RenderStrategy(str, enum.Enum):
    """How a navigation was satisfied. No boolean flags; the .value doubles as the
    output ``modality_used`` so the Chromium path stays output-compatible."""

    API_SPEC = "api_spec"
    HTTP_FIRST = "http_first"
    CHROMIUM = _MODALITY_AX


# Fetch bounds mirror agents/broken_link_crawler.py's proven caps.
_HTTP_FIRST_TIMEOUT_S = 12.0          # < the 15s nav timeout, so a slow site still falls back
_HTTP_FIRST_MAX_BYTES = 1_500_000
_HTTP_FIRST_MAX_REDIRECTS = 5
_HTTP_FIRST_UA = "Aztea-Site-Navigator/1.0 (http-first; for authorized navigation)"
_HTML_CTYPES = ("text/html", "application/xhtml+xml")


@dataclasses.dataclass(frozen=True)
class RawFetch:
    """A successful SSRF-safe fetch of any content-type (final URL + decoded text)."""

    final_url: str
    status: int
    content_type: str
    text: str


@dataclasses.dataclass(frozen=True)
class FetchResult:
    """A successful static HTML fetch: the final (post-redirect) URL and decoded HTML."""

    final_url: str
    status: int
    content_type: str
    html: str


def fetch_raw(url: str) -> RawFetch | None:
    """SSRF-safe GET of any content-type with manual per-hop redirect re-validation.

    Returns a RawFetch on a 2xx, or None on a 4xx/5xx, a blocked hop, too many
    redirects, or any transport error. Each hop is validated AND its IP pinned for the
    connect (validate_outbound_url does not follow redirects, so follow_redirects=False
    + a manual hop loop is the only correct pattern). Used by the HTML path and by
    /map's sitemap.xml/robots.txt fetches.
    """
    current = url
    try:
        with httpx.Client(
            follow_redirects=False, timeout=_HTTP_FIRST_TIMEOUT_S,
            headers={"User-Agent": _HTTP_FIRST_UA}, **_fetch_backend.httpx_kwargs(),
        ) as client:
            for _hop in range(_HTTP_FIRST_MAX_REDIRECTS + 1):
                try:
                    current = url_security.validate_outbound_url(current, "url")
                except ValueError:
                    _LOG.info("fetch blocked by SSRF policy: %s", current)
                    return None
                with outbound_session.pinned_ip_for_url(current):
                    with client.stream("GET", current) as resp:
                        if resp.is_redirect:
                            location = resp.headers.get("location")
                            if not location:
                                return None
                            current = urljoin(current, location)
                            continue
                        return _read_body(resp)
        return None  # exceeded _HTTP_FIRST_MAX_REDIRECTS
    except Exception:  # noqa: BLE001 — fetch is best-effort; the caller falls back
        _LOG.debug("fetch_raw failed for %s", url, exc_info=True)
        return None


def _read_body(resp: httpx.Response) -> RawFetch | None:
    """Gate a streamed response on status, then read up to the byte cap (any ctype)."""
    if resp.status_code >= 400:
        return None
    ctype = str(resp.headers.get("content-type", "")).lower()
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= _HTTP_FIRST_MAX_BYTES:
            break
    body = b"".join(chunks)[:_HTTP_FIRST_MAX_BYTES]
    return RawFetch(
        final_url=str(resp.url), status=resp.status_code, content_type=ctype,
        text=body.decode(resp.encoding or "utf-8", "replace"),
    )


def fetch_static_html(url: str) -> FetchResult | None:
    """SSRF-safe static HTML GET. None on a non-HTML type / 4xx-5xx / blocked / error
    so the navigator falls through to Chromium. Behavior unchanged from before; now a
    thin HTML gate over fetch_raw."""
    raw = fetch_raw(url)
    if raw is None or not any(c in raw.content_type for c in _HTML_CTYPES):
        return None
    return FetchResult(
        final_url=raw.final_url, status=raw.status, content_type=raw.content_type, html=raw.text,
    )
