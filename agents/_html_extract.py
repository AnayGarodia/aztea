"""HTML parsing for site_navigator's HTTP-first path: rows, the needs-browser
heuristic, clean markdown, and links — all from a fetched HTML string.

# OWNS: turning raw HTML into the same {role,name} row shape the accessibility-tree
#        pipeline emits, the "does this page need a real browser?" decision, and
#        readability-grade markdown.
# NOT OWNS: the network fetch (agents/_site_fetch.py), the LLM resolve, the commons.
# INVARIANTS:
#   * Rows use exactly the AX role alphabet {link,button,textbox,searchbox,combobox,
#     heading} so _extract_affordances / _build_site_map / _resolve_goal are reused
#     unchanged.
#   * needs_browser is tuned CONSERVATIVE — when unsure, return True. A false 'static'
#     silently returns thin data; a false 'browser' only costs a render.
# DECISIONS:
#   * trafilatura / markdownify are imported lazily INSIDE to_markdown: they are heavy
#     (lxml) and only needed when a markdown format is requested, so importing them at
#     module load would slow every app boot. Lazy here is the explicit intent.
"""

from __future__ import annotations

import dataclasses
import logging

from bs4 import BeautifulSoup

_LOG = logging.getLogger(__name__)

# Mirror the navigator's AX caps so HTTP-first and Chromium produce comparable maps.
_ROW_CAP = 400
_NAME_TRUNCATE = 160
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_INTERACTIVE_TAGS = ("a", "button", "input", "textarea", "select")
_AX_INPUT_ROLES = ("textbox", "searchbox", "combobox")
_EXPLICIT_ROLES = ("link", "button", "textbox", "searchbox", "combobox", "heading")
# <input type> -> AX role. Anything unlisted is a text field (textbox).
# A checkbox/radio is NOT a button — give them their real roles so the LLM doesn't
# reason about them as clickable buttons (and so this matches the Chromium aria_snapshot,
# which emits the true roles).
_INPUT_TYPE_TO_ROLE = {
    "search": "searchbox", "submit": "button", "button": "button", "reset": "button",
    "image": "button", "checkbox": "checkbox", "radio": "radio",
}
_SKIP_INPUT_TYPES = ("hidden",)

# needs-browser heuristic thresholds (named; the heuristic is the highest-risk part).
_MIN_BODY_TEXT_CHARS = 200
_MIN_PARSED_ROWS = 5
_MAX_SCRIPT_TO_TEXT_RATIO = 8.0
# Above this much rendered body text, trust the SSR content even with a big inlined
# JSON/hydration blob (e.g. __NEXT_DATA__) — don't misroute it to Chromium as script_heavy.
_SCRIPT_HEAVY_BODY_CEILING = 1_000
_SPA_MARKERS = (
    'id="root"', "id='root'", 'id="app"', "id='app'", 'id="__next"', "__NEXT_DATA__",
    "window.__NUXT__", "ng-version", "data-reactroot", "data-react-root", "data-server-rendered",
)


@dataclasses.dataclass(frozen=True)
class HtmlAnalysis:
    """One parse, all the signals: the rows plus the needs-browser verdict + reason."""

    rows: list[dict[str, str]]
    body_text_len: int
    needs_browser: bool
    reason: str


def _text_of(el: object) -> str:
    """Pure: the visible/label text of an element, trimmed."""
    getter = getattr(el, "get_text", None)
    text = getter(" ", strip=True) if callable(getter) else ""
    if text:
        return text
    get_attr = getattr(el, "get", None)
    if callable(get_attr):
        return str(get_attr("value") or get_attr("aria-label") or "").strip()
    return ""


def _input_name(el: object) -> str:
    """Pure: best label for an input/select (aria-label, placeholder, name)."""
    get_attr = getattr(el, "get", None)
    if not callable(get_attr):
        return ""
    return str(get_attr("aria-label") or get_attr("placeholder") or get_attr("name") or "").strip()


def _role_and_name(el: object) -> tuple[str, str]:
    """Pure: map one element to (AX role, name), or ("","") to skip it."""
    explicit = str(getattr(el, "get", lambda *_: "")("role") or "").strip().lower()
    if explicit in _EXPLICIT_ROLES:
        return explicit, _text_of(el)
    tag = getattr(el, "name", "")
    if tag == "a":
        return ("link", _text_of(el)) if el.get("href") else ("", "")  # type: ignore[union-attr]
    if tag == "button":
        return "button", _text_of(el)
    if tag in _HEADING_TAGS:
        return "heading", _text_of(el)
    if tag == "textarea":
        return "textbox", _input_name(el)
    if tag == "select":
        return "combobox", _input_name(el)
    if tag == "input":
        itype = str(el.get("type") or "text").strip().lower()  # type: ignore[union-attr]
        if itype in _SKIP_INPUT_TYPES:
            return "", ""
        role = _INPUT_TYPE_TO_ROLE.get(itype, "textbox")
        return role, (_text_of(el) if role == "button" else _input_name(el))
    return "", ""


def _rows_from_soup(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Pure-ish: {role,name} rows in document order, capped, matching the AX alphabet."""
    rows: list[dict[str, str]] = []
    for el in soup.find_all([*_INTERACTIVE_TAGS, *_HEADING_TAGS]):
        if len(rows) >= _ROW_CAP:
            break
        role, name = _role_and_name(el)
        # Keep named affordances; keep inputs even when unnamed (they're actionable).
        if role and (name or role in _AX_INPUT_ROLES):
            rows.append({"role": role, "name": name[:_NAME_TRUNCATE]})
    return rows


def _needs_browser(html: str, row_count: int, body_text_len: int, script_text_len: int) -> tuple[bool, str]:
    """Pure: (needs_browser, named_reason). Conservative — any trip wins.

    A bare SPA marker is NOT decisive on its own (SSR Next.js ships a full body with a
    ``__NEXT_DATA__`` marker), so it must AND with a thin body. The row floor and the
    script-to-text ratio catch client-rendered shells the marker set misses.
    """
    has_marker = any(m in html for m in _SPA_MARKERS)
    if body_text_len < _MIN_BODY_TEXT_CHARS and has_marker:
        return True, "spa_shell"
    if row_count < _MIN_PARSED_ROWS:
        return True, "too_few_rows"
    if 0 < body_text_len < _SCRIPT_HEAVY_BODY_CEILING and (script_text_len / body_text_len) > _MAX_SCRIPT_TO_TEXT_RATIO:
        return True, "script_heavy"
    if body_text_len < _MIN_BODY_TEXT_CHARS:
        return True, "thin_body"
    return False, "static_ok"


def analyze_html(html: str) -> HtmlAnalysis:
    """One BeautifulSoup parse -> rows + the needs-browser verdict (with named reason)."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows = _rows_from_soup(soup)
    # script length BEFORE stripping; body text EXCLUDING script/style/noscript, since
    # get_text() otherwise counts a big inlined JSON blob (e.g. __NEXT_DATA__) as
    # "body content" and masks a client-rendered shell.
    script_text_len = sum(len(s.get_text() or "") for s in soup.find_all("script"))
    body = soup.body or soup
    for tag in body.find_all(["script", "style", "noscript"]):
        tag.extract()
    body_text_len = len(body.get_text(" ", strip=True)) if body else 0
    needs, reason = _needs_browser(html or "", len(rows), body_text_len, script_text_len)
    return HtmlAnalysis(rows=rows, body_text_len=body_text_len, needs_browser=needs, reason=reason)


def to_markdown(html: str) -> str:
    """Clean, LLM-ready markdown.

    Default: trafilatura (readability-grade main-content extraction) per review T1.
    Fallback: markdownify over a boilerplate-stripped body so the path never hard-fails
    if trafilatura returns nothing (some shells) or is unavailable.
    """
    try:
        import trafilatura  # lazy: heavy (lxml); only when a markdown format is requested

        extracted = trafilatura.extract(
            html, output_format="markdown", include_links=True, include_tables=True,
        )
        if extracted and extracted.strip():
            return extracted.strip()
    except Exception:  # noqa: BLE001 — fall through to the BS4/markdownify path, logged
        _LOG.debug("trafilatura extraction failed; using markdownify fallback", exc_info=True)
    return _fallback_markdown(html)


def _fallback_markdown(html: str) -> str:
    """Boilerplate-strip then markdownify; plain text if markdownify is unavailable."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "noscript", "svg"]):
        tag.decompose()
    target = soup.body or soup
    try:
        from markdownify import markdownify as _md  # lazy: paired with the markdown path

        return _md(str(target), heading_style="ATX").strip()
    except Exception:  # noqa: BLE001 — last-resort plain text, logged
        _LOG.debug("markdownify unavailable; returning plain text", exc_info=True)
        return target.get_text("\n", strip=True)


def title_of(html: str) -> str:
    """Pure: the page <title> text, trimmed, or ''."""
    soup = BeautifulSoup(html or "", "html.parser")
    return soup.title.get_text(strip=True) if soup.title else ""


def extract_links(html: str, *, limit: int = 100) -> list[str]:
    """De-duplicated href list for the ``links`` output format (skips anchors/js:)."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:")) or href in seen:
            continue
        seen.add(href)
        out.append(href)
        if len(out) >= limit:
            break
    return out
