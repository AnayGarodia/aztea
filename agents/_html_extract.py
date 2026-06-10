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
#   * Markdown is HYBRID (to_markdown): trafilatura (article extractor) for article/doc
#     pages, and a clean-DOM markdownify pass for feed/listing/app pages where
#     trafilatura returns token-soup. Routed automatically by _is_fragmented. This is
#     why a page like Hacker News no longer comes back as '| --- |' table scaffolding.
#   * trafilatura / markdownify are imported lazily INSIDE the markdown helpers: they are
#     heavy (lxml) and only needed when a markdown format is requested, so importing them
#     at module load would slow every app boot. Lazy here is the explicit intent.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

_LOG = logging.getLogger(__name__)

# Boilerplate stripped before the clean-DOM markdown pass (the non-article path).
_BOILERPLATE_SELECTORS = ("nav", "footer", "aside", "[aria-hidden=true]", "[hidden]")
# Fragmentation thresholds: trafilatura is an ARTICLE extractor, so on a feed/listing/
# app page (Hacker News, search results, dashboards) its markdown collapses to one-token-
# per-line soup. Measured: HN ~0.93 short-line fraction / avg ~8 chars; real articles
# (Wikipedia, MDN) <0.2 / >100 chars. Above these, route to the clean-DOM path instead.
_FRAGMENT_SHORT_LINE_CHARS = 25
_FRAGMENT_SHORT_LINE_FRACTION = 0.6
_FRAGMENT_MIN_LINES = 8
# When trafilatura renders a layout-table page (e.g. HN) it can emit markdown-TABLE rows
# (lines starting with '|') rather than short-token soup. A page dominated by pipe rows
# is a layout table mis-read as a data table -> route to the clean-DOM path, which
# unwraps layout tables. A real article with one data table stays well under this.
_FRAGMENT_PIPE_LINE_FRACTION = 0.35

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


def _needs_browser(
    html: str, row_count: int, body_text_len: int, script_text_len: int, script_count: int,
) -> tuple[bool, str]:
    """Pure: (needs_browser, named_reason). Conservative — any trip wins.

    A bare SPA marker is NOT decisive on its own (SSR Next.js ships a full body with a
    ``__NEXT_DATA__`` marker), so it must AND with a thin body. The row floor and the
    script-to-text ratio catch client-rendered shells the marker set misses.

    ``script_count`` (number of <script> elements, inline OR src) is decisive the other
    way: with ZERO scripts a render cannot add anything — JS is the only thing a browser
    executes that HTTP can't see — so a thin/sparse but script-free page (example.com)
    is served statically instead of paying a pointless Chromium launch.
    """
    has_marker = any(m in html for m in _SPA_MARKERS)
    if body_text_len < _MIN_BODY_TEXT_CHARS and has_marker:
        return True, "spa_shell"
    if script_count == 0:
        return False, "static_ok"
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
    scripts = soup.find_all("script")
    script_text_len = sum(len(s.get_text() or "") for s in scripts)
    body = soup.body or soup
    for tag in body.find_all(["script", "style", "noscript"]):
        tag.extract()
    body_text_len = len(body.get_text(" ", strip=True)) if body else 0
    needs, reason = _needs_browser(html or "", len(rows), body_text_len, script_text_len, len(scripts))
    return HtmlAnalysis(rows=rows, body_text_len=body_text_len, needs_browser=needs, reason=reason)


def to_markdown(html: str) -> str:
    """Clean, LLM-ready markdown — hybrid so it works on articles AND listings.

    Articles/docs/blogs: trafilatura's readability-grade main-content extraction (strips
    nav, sidebars, related-links — best in class). Feed / listing / app pages (Hacker
    News, search results, dashboards): trafilatura has no "article" to find and returns
    token-soup, so when its output is fragmented (see _is_fragmented) we instead clean
    the DOM and convert THAT to markdown, preserving links + headings without the layout-
    table '| --- |' scaffolding markdownify would otherwise emit.
    """
    article = _normalize_markdown(_trafilatura_markdown(html))
    if article and not _is_fragmented(article):
        return article
    generic = _clean_dom_markdown(html)
    return generic or article


def _trafilatura_markdown(html: str) -> str:
    """The article path. Empty string on failure / no main content (caller routes on)."""
    try:
        import trafilatura  # lazy: heavy (lxml); only when a markdown format is requested

        out = trafilatura.extract(
            html, output_format="markdown", include_links=True, include_tables=True,
        )
        return (out or "").strip()
    except Exception:  # noqa: BLE001 — caller falls back to the clean-DOM path, logged
        _LOG.debug("trafilatura extraction failed", exc_info=True)
        return ""


def _is_fragmented(md_text: str) -> bool:
    """Pure: True when markdown is token-soup — a listing/app page the article extractor
    mangled — so the clean-DOM path should serve it. Tuned on HN vs Wikipedia/MDN."""
    lines = [ln for ln in md_text.splitlines() if ln.strip()]
    if len(lines) < _FRAGMENT_MIN_LINES:
        return False
    short = sum(1 for ln in lines if len(ln.strip()) < _FRAGMENT_SHORT_LINE_CHARS) / len(lines)
    avg = sum(len(ln) for ln in lines) / len(lines)
    pipe = sum(1 for ln in lines if ln.lstrip().startswith("|")) / len(lines)
    return (
        short > _FRAGMENT_SHORT_LINE_FRACTION
        or avg < _FRAGMENT_SHORT_LINE_CHARS
        or pipe > _FRAGMENT_PIPE_LINE_FRACTION
    )


def _unwrap_layout_tables(soup: BeautifulSoup) -> None:
    """Side-effect: rename LAYOUT tables (no <th>) to plain divs so markdownify emits
    flowing content with links intact instead of '| --- |' scaffolding. Tables WITH a
    <th> are left alone — those are real data tables worth keeping as markdown tables."""
    for table in soup.find_all("table"):
        if table.find("th"):
            continue
        for el in table.find_all(["table", "thead", "tbody", "tfoot", "tr", "td", "th"]):
            el.name = "div"
        table.name = "div"


def _clean_dom_markdown(html: str) -> str:
    """Strip chrome + unwrap layout tables, then HTML->markdown. The non-article path,
    so feed/listing/app pages still produce clean, link-preserving markdown."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        tag.decompose()
    for selector in _BOILERPLATE_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()
    _unwrap_layout_tables(soup)
    target = soup.body or soup
    try:
        from markdownify import markdownify as _md  # lazy: paired with the markdown path

        text = _md(str(target), heading_style="ATX", bullets="-")
    except Exception:  # noqa: BLE001 — last-resort plain text, logged
        _LOG.debug("markdownify unavailable; returning plain text", exc_info=True)
        return target.get_text("\n", strip=True)
    return _normalize_markdown(text)


def _normalize_markdown(text: str) -> str:
    """Pure: trim trailing space, drop orphan pipe-only lines, collapse blank runs,
    and re-join punctuation that trafilatura orphans onto its own paragraph after an
    inline-code span (``` `X`\\n\\n, `Y` ``` -> ``` `X`, `Y` ```). The join is scoped to
    a preceding backtick so leading-comma code styles inside fences are never touched."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n(?:[ \t]*\|[ \t]*)+\n", "\n", text)
    text = re.sub(r"(?<=`)\n{1,2}(?=[,.;:])", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def strip_images(markdown: str) -> str:
    """Pure: remove markdown image embeds (``![alt](url)``) and collapse the blank lines
    left behind. For the 'read the article TEXT' path, images are noise — they add no
    value to an LLM and render as broken-icon boxes — so the followed-article markdown is
    run through this. The main scrape markdown keeps its images."""
    out = _MD_IMAGE.sub("", markdown or "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


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


# Anchor-text floor for "this is a content link, not chrome". Titles/article links carry
# substantial text (HN story titles ~30-80 chars); nav carries short text ("past",
# "hide", "60 comments", "login"), so this cleanly separates the two on feed pages.
_MIN_CONTENT_ANCHOR_CHARS = 20
# Feed pages (HN-style) link stories OFF-site, and real story titles can be short
# ("Claude Fable 5" is 14 chars). Chrome links ("169 comments", "5 hours ago") stay
# on the feed's own host, so an off-host anchor earns a lower text floor.
_MIN_EXTERNAL_ANCHOR_CHARS = 12
# A domain-ish anchor (e.g. "(github.com/apple)" or "twitter.com/user" beside a feed
# title) can pass either floor but is NOT content — it links to a site/submissions page.
# The optional /path suffix matters: "twitter.com/richardssutton" is still a domain
# annotation, not an article title.
_DOMAIN_LIKE_ANCHOR = re.compile(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", re.IGNORECASE)
# Anchors inside these containers are navigation chrome regardless of text length
# (external "Privacy Policy"-style footer links would otherwise pass the lower floor).
_CHROME_CONTAINERS = ("nav", "footer")


def _content_anchor_floor(url: str, base_host: str) -> int:
    """Pure: the minimum anchor-text length for ``url`` to count as content."""
    host = (urlparse(url).hostname or "").lower()
    if base_host and host and host != base_host:
        return _MIN_EXTERNAL_ANCHOR_CHARS
    return _MIN_CONTENT_ANCHOR_CHARS


def content_links(html: str, *, limit: int, base_url: str = "") -> list[dict[str, str]]:
    """Resolved [{text, url}] for the page's CONTENT links — the ones worth following
    to read the thing behind a feed/index (the story titles), not the nav chrome.

    Selection: anchor text >= the host-aware floor (_content_anchor_floor), not
    domain-like, not inside nav/footer, http(s) after resolving against base_url,
    deduped, document order, capped at ``limit``. Used by the follow-links expansion
    so a Hacker News scrape can return each linked article's content, not just the
    headline list.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    base_host = (urlparse(base_url).hostname or "").lower()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        text = anchor.get_text(" ", strip=True)
        if " " not in text and _DOMAIN_LIKE_ANCHOR.match(text):
            continue  # a domain annotation, not an article
        if anchor.find_parent(list(_CHROME_CONTAINERS)) is not None:
            continue
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base_url, href)
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        if len(text) < _content_anchor_floor(url, base_host):
            continue
        seen.add(url)
        out.append({"text": text[:_NAME_TRUNCATE], "url": url})
        if len(out) >= limit:
            break
    return out
