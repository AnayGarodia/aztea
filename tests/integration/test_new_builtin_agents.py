"""Integration tests for dns_inspector and the curated demo built-ins
(lighthouse_auditor, accessibility_auditor).

2026-05-26 platform-pivot cull: security_headers_grader,
broken_link_crawler, pdf_document_parser, and web_search are now
sunset. Tests for the sunset agents are skipped because direct calls
to /registry/agents/{id}/call return 410 Gone for sunset agents; the
agent modules themselves still exist for old job-ID resolution.
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from tests.integration.support import *  # noqa: F403

DNS_INSPECTOR_AGENT_ID = server._DNS_INSPECTOR_AGENT_ID
LIGHTHOUSE_AUDITOR_AGENT_ID = server._LIGHTHOUSE_AUDITOR_AGENT_ID
ACCESSIBILITY_AUDITOR_AGENT_ID = server._ACCESSIBILITY_AUDITOR_AGENT_ID
SECURITY_HEADERS_GRADER_AGENT_ID = server._SECURITY_HEADERS_GRADER_AGENT_ID
BROKEN_LINK_CRAWLER_AGENT_ID = server._BROKEN_LINK_CRAWLER_AGENT_ID
PDF_DOCUMENT_PARSER_AGENT_ID = server._PDF_DOCUMENT_PARSER_AGENT_ID
WEB_SEARCH_AGENT_ID = server._WEB_SEARCH_AGENT_ID

_CULL_SKIP_REASON = (
    "Sunset 2026-05-26 platform-pivot cull: /registry/agents/{id}/call "
    "returns 410 Gone for sunset agent IDs. Re-enable when this agent "
    "returns to CURATED_PUBLIC_BUILTIN_AGENT_IDS."
)


def test_dns_inspector_basic(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    mock_socket_info = [(2, 1, 6, "", ("93.184.216.34", 0))]

    mock_conn = MagicMock()
    mock_ssl_sock = MagicMock()
    mock_ssl_sock.getpeercert.return_value = {
        "subject": ((("commonName", "example.com"),),),
        "issuer": ((("organizationName", "DigiCert"),),),
        "notAfter": "Jan 01 00:00:00 2030 GMT",
        "subjectAltName": [("DNS", "example.com"), ("DNS", "www.example.com")],
    }
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)

    mock_http_resp = MagicMock()
    mock_http_resp.status = 200
    mock_http_resp.headers = {}
    mock_http_resp.__enter__ = lambda s: s
    mock_http_resp.__exit__ = MagicMock(return_value=False)

    with patch("agents.dns_inspector.validate_outbound_url", return_value="https://example.com"), \
         patch("agents.dns_inspector.socket.getaddrinfo", return_value=mock_socket_info), \
         patch("agents.dns_inspector.socket.create_connection", return_value=mock_conn), \
         patch("agents.dns_inspector.ssl.create_default_context") as mock_ctx, \
         patch("agents.dns_inspector.urllib.request.urlopen", return_value=mock_http_resp):
        mock_ctx.return_value.wrap_socket.return_value.__enter__ = lambda s: mock_ssl_sock
        mock_ctx.return_value.wrap_socket.return_value.__exit__ = MagicMock(return_value=False)

        resp = client.post(
            f"/registry/agents/{DNS_INSPECTOR_AGENT_ID}/call",
            json={"domains": ["example.com"], "checks": ["dns", "ssl", "http"]},
            headers=_auth_headers(caller["raw_api_key"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()["output"]
    assert "results" in body
    assert len(body["results"]) == 1
    assert body["results"][0]["domain"] == "example.com"
    assert body["results"][0]["a_records"] == ["93.184.216.34"]
    assert isinstance(body["billing_units_actual"], int)
    assert body["billing_units_actual"] == 1


# ---------------------------------------------------------------------------
# YC-demo agents — contract tests. We mock the heavy external dep (lighthouse
# subprocess, Playwright, httpx, Brave API) and assert the dispatch wiring +
# output envelope shape. End-to-end smoke against real systems is covered by
# the manual buyer-surface checklist (docs/runbooks/buyer-surface-smoke-test.md).
# ---------------------------------------------------------------------------


def test_lighthouse_auditor_contract(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    fake_report = {
        "finalUrl": "https://example.com/",
        "fetchTime": "2026-05-09T12:00:00.000Z",
        "lighthouseVersion": "11.0.0",
        "categories": {
            "performance": {"score": 0.71, "auditRefs": [{"id": "largest-contentful-paint"}]},
            "accessibility": {"score": 0.92, "auditRefs": []},
            "best-practices": {"score": 0.96, "auditRefs": []},
            "seo": {"score": 1.0, "auditRefs": []},
        },
        "audits": {
            "largest-contentful-paint": {"numericValue": 3400, "score": 0.4, "title": "LCP"},
            "first-contentful-paint": {"numericValue": 1100, "score": 0.9, "title": "FCP"},
            "cumulative-layout-shift": {"numericValue": 0.04, "score": 0.95},
            "total-blocking-time": {"numericValue": 280, "score": 0.7},
            "interactive": {"numericValue": 4800, "score": 0.6},
            "speed-index": {"numericValue": 2900, "score": 0.7},
            "uses-optimized-images": {
                "title": "Efficiently encode images",
                "description": "Images can be optimised.",
                "numericValue": 1200,
                "score": 0.5,
                "details": {"type": "opportunity", "overallSavingsMs": 1200},
            },
        },
    }

    completed = MagicMock(returncode=0, stdout="", stderr="")

    def _fake_open(*_args, **_kwargs):
        from io import StringIO
        return StringIO(json.dumps(fake_report))

    with patch("agents.lighthouse_auditor._resolve_lighthouse_bin", return_value="/usr/bin/lighthouse"), \
         patch("agents.lighthouse_auditor.validate_outbound_url", return_value="https://example.com"), \
         patch("agents.lighthouse_auditor.subprocess.run", return_value=completed), \
         patch("agents.lighthouse_auditor.os.path.exists", return_value=True), \
         patch("builtins.open", _fake_open), \
         patch("agents.lighthouse_auditor.os.unlink"):
        resp = client.post(
            f"/registry/agents/{LIGHTHOUSE_AUDITOR_AGENT_ID}/call",
            json={"url": "https://example.com", "strategy": "mobile"},
            headers=_auth_headers(caller["raw_api_key"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()["output"]
    assert body["scores"]["performance"] == 71
    assert body["scores"]["seo"] == 100
    assert body["metrics"]["lcp_ms"] == 3400
    assert body["metrics"]["cls"] == 0.04
    assert any(o["id"] == "uses-optimized-images" for o in body["top_opportunities"])
    assert body["billing_units_actual"] == 1


def test_accessibility_auditor_contract(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    fake_axe_payload = {
        "ok": True,
        "result": {
            "testEngine": {"name": "axe-core", "version": "4.8.4"},
            "violations": [
                {
                    "id": "color-contrast",
                    "impact": "serious",
                    "tags": ["wcag2aa"],
                    "help": "Elements must have sufficient color contrast",
                    "helpUrl": "https://dequeuniversity.com/rules/axe/4.8/color-contrast",
                    "nodes": [
                        {
                            "target": [".cta-secondary"],
                            "html": "<a class=\"cta-secondary\">Read</a>",
                            "failureSummary": "Insufficient contrast 3.2:1",
                        }
                    ],
                }
            ],
            "passes": [{}, {}, {}],
            "incomplete": [{}],
        },
    }

    fake_page = MagicMock()
    fake_page.title.return_value = "Example Domain"
    fake_page.url = "https://example.com/"
    fake_page.evaluate.return_value = fake_axe_payload

    fake_context = MagicMock()
    fake_context.new_page.return_value = fake_page

    fake_browser = MagicMock()
    fake_browser.new_context.return_value = fake_context

    fake_pw = MagicMock()
    fake_pw.chromium.launch.return_value = fake_browser

    class _SyncPlaywrightCtx:
        def __enter__(self):
            return fake_pw

        def __exit__(self, *_args):
            return False

    with patch("agents.accessibility_auditor.url_security.validate_outbound_url", return_value="https://example.com"):
        with patch.dict("sys.modules", {"playwright.sync_api": MagicMock(sync_playwright=lambda: _SyncPlaywrightCtx())}):
            resp = client.post(
                f"/registry/agents/{ACCESSIBILITY_AUDITOR_AGENT_ID}/call",
                json={"url": "https://example.com"},
                headers=_auth_headers(caller["raw_api_key"]),
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()["output"]
    assert body["axe_version"] == "4.8.4"
    assert body["totals"]["serious"] == 1
    assert body["totals"]["passes"] == 3
    assert len(body["violations"]) == 1
    assert body["violations"][0]["id"] == "color-contrast"
    assert body["billing_units_actual"] == 1


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_security_headers_grader_contract(client):
    """Security Headers Grader is sunset from the callable public catalog."""
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    resp = client.post(
        f"/registry/agents/{SECURITY_HEADERS_GRADER_AGENT_ID}/call",
        json={"url": "https://example.com"},
        headers=_auth_headers(caller["raw_api_key"]),
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["error"] == "agent.sunset"


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_broken_link_crawler_contract(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    page_html = b"""
    <html><head><title>Seed</title></head>
    <body>
      <a href="/ok">OK page</a>
      <a href="/missing">Missing page</a>
      <img src="/hero.jpg" alt="">
      <img src="/banner.jpg" alt="banner">
      <script src="http://example.com/insecure.js"></script>
    </body></html>
    """

    async def _fake_crawl(*_a, **_kw):
        # Use the agent's own helper structure: returning a synthetic shape that
        # matches the real output. We bypass the network entirely.
        return {
            "seed_url": "https://example.com",
            "origin": "https://example.com",
            "pages_crawled": 1,
            "links_checked": 2,
            "broken_links": [
                {
                    "url": "https://example.com/missing",
                    "status_code": 404,
                    "found_on": "https://example.com",
                    "reason": "HTTP 404",
                }
            ],
            "redirect_chains": [],
            "mixed_content": [
                {"page_url": "https://example.com", "asset_url": "http://example.com/insecure.js"}
            ],
            "missing_alt_text": [
                {"page_url": "https://example.com", "img_src": "https://example.com/hero.jpg"}
            ],
            "summary": {
                "broken_count": 1,
                "redirects_count": 0,
                "mixed_content_count": 1,
                "missing_alt_count": 1,
            },
            "billing_units_actual": 1,
        }

    _ = page_html  # keep the literal so a future real-test variant can reuse

    with patch("agents.broken_link_crawler.validate_outbound_url", return_value="https://example.com"), \
         patch("agents.broken_link_crawler._crawl", _fake_crawl):
        resp = client.post(
            f"/registry/agents/{BROKEN_LINK_CRAWLER_AGENT_ID}/call",
            json={"url": "https://example.com", "max_pages": 5, "max_depth": 1},
            headers=_auth_headers(caller["raw_api_key"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()["output"]
    assert body["pages_crawled"] == 1
    assert body["summary"]["broken_count"] == 1
    assert body["summary"]["mixed_content_count"] == 1
    assert body["summary"]["missing_alt_count"] == 1
    assert body["billing_units_actual"] == 1


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_pdf_document_parser_contract(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    # Minimal valid PDF — pymupdf can open this.
    pdf_bytes = (
        b"%PDF-1.1\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length 44 >> stream\nBT /F1 24 Tf 100 700 Td (Hello PDF) Tj ET\nendstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"xref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000115 00000 n \n0000000220 00000 n \n0000000310 00000 n \n"
        b"trailer << /Size 6 /Root 1 0 R >>\nstartxref\n380\n%%EOF\n"
    )

    with patch("agents.pdf_document_parser.validate_outbound_url", return_value="https://example.com/test.pdf"), \
         patch("agents.pdf_document_parser._fetch_pdf", return_value=(pdf_bytes, None)):
        resp = client.post(
            f"/registry/agents/{PDF_DOCUMENT_PARSER_AGENT_ID}/call",
            json={"url": "https://example.com/test.pdf", "include_tables": False},
            headers=_auth_headers(caller["raw_api_key"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()["output"]
    # If pymupdf isn't installed in the test env we still expect a structured
    # error rather than a 500.
    if "error" in body:
        assert body["error"]["code"] == "pdf_document_parser.runtime_missing"
        return
    assert body["page_count"] >= 1
    assert "Hello PDF" in body["text"] or body["pages_returned"] >= 1
    assert body["billing_units_actual"] >= 1


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_web_search_missing_query_returns_structured_error(client):
    """Web Search is sunset from the public callable catalog."""
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    resp = client.post(
        f"/registry/agents/{WEB_SEARCH_AGENT_ID}/call",
        json={"query": "", "count": 5},
        headers=_auth_headers(caller["raw_api_key"]),
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["error"] == "agent.sunset"


@pytest.mark.skip(reason=_CULL_SKIP_REASON)
def test_web_search_happy_path_contract(client):
    """Web Search stays wired for receipts but is no longer callable."""
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    resp = client.post(
        f"/registry/agents/{WEB_SEARCH_AGENT_ID}/call",
        json={"query": "aztea ai", "count": 3},
        headers=_auth_headers(caller["raw_api_key"]),
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["error"] == "agent.sunset"
