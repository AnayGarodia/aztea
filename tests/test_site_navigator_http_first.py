"""Unit tests for the HTTP-first path helpers (agents/_site_fetch + _html_extract).

Covers the four needs-browser signal classes, the HTML->AX-role mapping, markdown +
links extraction, and the SSRF gate on the static fetch (no network — IP literals
trip the check directly).
"""

from __future__ import annotations

from agents import _html_extract as he
from agents import _site_fetch as sf
from agents import site_navigator as sn


def _ssr_page() -> str:
    paragraph = "<p>" + ("Real readable content here. " * 20) + "</p>"
    links = "".join(f'<a href="/plan{i}">Plan number {i}</a>' for i in range(6))
    return f"<html><body><h1>Pricing</h1>{paragraph}{links}</body></html>"


# --------------------------------------------------------------------------- needs-browser heuristic
def test_analyze_html_static_ssr_page_does_not_need_browser():
    a = he.analyze_html(_ssr_page())
    assert a.needs_browser is False and a.reason == "static_ok"
    assert len(a.rows) >= 6


def test_analyze_html_flags_empty_spa_shell():
    shell = '<html><body><div id="root"></div><script>window.__NEXT_DATA__={"x":1}</script></body></html>'
    a = he.analyze_html(shell)
    assert a.needs_browser is True and a.reason == "spa_shell"


def test_analyze_html_flags_too_few_rows():
    page = "<html><body><p>" + ("word " * 60) + "</p><a href='/x'>Only link</a></body></html>"
    a = he.analyze_html(page)
    assert a.needs_browser is True and a.reason == "too_few_rows"


def test_analyze_html_flags_script_heavy():
    body = "<p>" + ("word " * 70) + "</p>" + "".join(
        f'<a href="/x{i}">Item number {i} here</a>' for i in range(8)
    )
    page = f"<html><body>{body}<script>{'x' * 5000}</script></body></html>"
    a = he.analyze_html(page)
    assert a.needs_browser is True and a.reason == "script_heavy"


# --------------------------------------------------------------------------- HTML -> rows
def test_html_to_rows_maps_the_ax_role_alphabet():
    html = (
        '<a href="/x">Link</a><button>Go</button>'
        '<input type="search" aria-label="Find"><input type="text" placeholder="Name">'
        '<select name="country"></select><h2>Title</h2>'
        '<input type="hidden" name="csrf">'  # hidden input is skipped
    )
    roles = [r["role"] for r in he.analyze_html(html).rows]
    assert set(roles) == {"link", "button", "searchbox", "textbox", "combobox", "heading"}
    # the hidden input contributed no row
    assert roles.count("textbox") == 1


def test_html_to_rows_keeps_unnamed_inputs_drops_unnamed_links():
    html = '<a href="/x"></a><input type="text">'  # nameless link dropped, nameless input kept
    rows = he.analyze_html(html).rows
    assert {"role": "textbox", "name": ""} in rows
    assert all(r["role"] != "link" for r in rows)


# --------------------------------------------------------------------------- markdown + links
def test_to_markdown_extracts_main_content():
    html = (
        "<html><body><article><h1>Hello</h1><p>This is the main content of the page. "
        + ("More detail follows. " * 30)
        + "</p></article><nav>nav junk links</nav></body></html>"
    )
    md = he.to_markdown(html)
    assert "main content" in md


def test_extract_links_dedups_and_skips_anchors_and_js():
    html = (
        '<a href="/a">A</a><a href="/a">dup</a><a href="#frag">f</a>'
        '<a href="javascript:x()">j</a><a href="/b">B</a>'
    )
    assert he.extract_links(html) == ["/a", "/b"]


# --------------------------------------------------------------------------- fetch SSRF gate
def test_fetch_static_html_blocks_ssrf_first_hop_without_network():
    # IP literals trip validate_outbound_url directly — no DNS, no socket, fast None.
    assert sf.fetch_static_html("http://169.254.169.254/latest/meta-data/") is None
    assert sf.fetch_static_html("http://127.0.0.1:8000/admin") is None
    assert sf.fetch_static_html("http://[::1]/") is None


def test_render_strategy_chromium_value_matches_ax_modality():
    # The output 'modality_used' for the Chromium path must be unchanged.
    assert sf.RenderStrategy.CHROMIUM.value == "accessibility_tree"


# --------------------------------------------------------------------------- run() branching
def _no_browser(monkeypatch):
    """Stub the Chromium path so a unit test can prove a no-browser path served the
    call: _import_playwright flips a sentinel + returns tool_unavailable; commons +
    receipts are off so the test stays isolated from the real DB/signing."""
    called = {"pw": False}

    def _boom():
        called["pw"] = True
        return sn._err("site_navigator.tool_unavailable", "browser path reached")

    monkeypatch.setattr(sn, "_import_playwright", _boom)
    monkeypatch.setattr(sn.feature_flags, "sitemap_commons_enabled", lambda: False)
    monkeypatch.setattr(sn.feature_flags, "observation_receipts_enabled", lambda: False)
    return called


def test_run_flags_off_takes_chromium_path(monkeypatch):
    # Default flags off -> neither no-browser branch runs -> the (stubbed) Chromium path.
    called = _no_browser(monkeypatch)
    out = sn.run({"url": "https://example.com/", "goal": "x"})
    assert out["error"]["code"] == "site_navigator.tool_unavailable"
    assert called["pw"] is True


def test_run_http_first_serves_static_without_browser(monkeypatch):
    called = _no_browser(monkeypatch)
    monkeypatch.setattr(sn.feature_flags, "http_first_enabled", lambda: True)
    monkeypatch.setattr(sn.feature_flags, "api_discovery_enabled", lambda: False)
    fetched = sf.FetchResult(final_url="https://example.com/", status=200,
                             content_type="text/html", html=_ssr_page())
    monkeypatch.setattr(sn._site_fetch, "fetch_static_html", lambda url: fetched)
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: None)
    out = sn.run({"url": "https://example.com/", "goal": "list the plans"})
    assert out["source"] == "http_first" and out["modality_used"] == "http_first"
    assert out["cost_class"] == "cheap" and called["pw"] is False


def test_run_http_first_falls_back_to_chromium_on_spa_shell(monkeypatch):
    called = _no_browser(monkeypatch)
    monkeypatch.setattr(sn.feature_flags, "http_first_enabled", lambda: True)
    monkeypatch.setattr(sn.feature_flags, "api_discovery_enabled", lambda: False)
    shell = sf.FetchResult(
        final_url="https://example.com/", status=200, content_type="text/html",
        html='<html><body><div id="root"></div><script>window.__NEXT_DATA__={"x":1}</script></body></html>',
    )
    monkeypatch.setattr(sn._site_fetch, "fetch_static_html", lambda url: shell)
    out = sn.run({"url": "https://example.com/", "goal": "x"})
    assert out["error"]["code"] == "site_navigator.tool_unavailable"  # fell through
    assert called["pw"] is True


def test_run_markdown_format_makes_goal_optional(monkeypatch):
    _no_browser(monkeypatch)
    monkeypatch.setattr(sn.feature_flags, "http_first_enabled", lambda: True)
    monkeypatch.setattr(sn.feature_flags, "api_discovery_enabled", lambda: False)
    fetched = sf.FetchResult(final_url="https://example.com/", status=200,
                             content_type="text/html", html=_ssr_page())
    monkeypatch.setattr(sn._site_fetch, "fetch_static_html", lambda url: fetched)
    out = sn.run({"url": "https://example.com/", "formats": ["markdown"]})  # no goal
    assert "error" not in out and out["source"] == "http_first"
    assert "markdown" in out and isinstance(out["markdown"], str)


def test_run_api_spec_replay_serves_without_browser(monkeypatch):
    from core.site_maps import normalize
    body = {"tiers": [{"name": "Pro", "price": 20}]}
    spec = {
        "api_spec_id": "sapi_x", "method": "GET", "endpoint_scheme": "https",
        "endpoint_host": "api.example.com", "endpoint_port": None,
        "path_template": "/v2/pricing", "query_template": "", "status": "active",
        "response_fingerprint": normalize.response_shape_fingerprint(body),
        "last_validated_at": "2000-01-01T00:00:00+00:00", "author_did": "did:web:x",
    }
    called = _no_browser(monkeypatch)
    monkeypatch.setattr(sn.feature_flags, "api_discovery_enabled", lambda: True)
    monkeypatch.setattr(sn._commons, "find_reusable_api_spec", lambda url: spec)
    monkeypatch.setattr(sn._api_discovery, "replay", lambda endpoint, method="GET": body)
    monkeypatch.setattr(sn._commons_store, "bump_api_spec_hit", lambda *a, **k: None)
    monkeypatch.setattr(sn, "llm_complete", lambda *a, **k: None)  # degrade -> raw body
    out = sn.run({"url": "https://www.example.com/pricing", "goal": "tiers"})
    assert out["source"] == "api_spec" and out["reuse"]["reused"] is True
    assert out["cost_class"] == "cheap" and out["result"] == body  # past-TTL revalidated by shape
    assert called["pw"] is False
