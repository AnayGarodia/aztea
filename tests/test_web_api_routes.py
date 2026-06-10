"""Route tests for the public web API (Phase D) + /web/verify (Phase F).

Mounts the router on a bare FastAPI app (no full server startup) with a no-op limiter,
so these are fast and isolated. The engine is mocked for scrape/map; /web/verify is
exercised end-to-end (real sign -> route -> verify) with no DB.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import crypto
from server.routes import web_api


class _NoopLimiter:
    def limit(self, *_a, **_k):
        def _deco(fn):
            return fn
        return _deco


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(web_api.create_router(limiter=_NoopLimiter(), optional_api_key=lambda: None))
    return TestClient(app)


def test_scrape_gated_off_returns_503(monkeypatch):
    monkeypatch.delenv("AZTEA_WEB_API_ENABLED", raising=False)
    r = _client().post("/scrape", json={"url": "https://example.com"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "web_api.disabled"


def test_scrape_returns_firecrawl_shape(monkeypatch):
    monkeypatch.setenv("AZTEA_WEB_API_ENABLED", "1")
    monkeypatch.setattr(web_api._site_navigator, "run", lambda payload: {
        "url": "https://example.com", "requested_url": "https://example.com",
        "result": {"x": 1}, "markdown": "# Hi", "site_map": {"title": "Example"},
        "source": "http_first", "cost_class": "cheap",
    })
    body = _client().post("/scrape", json={"url": "https://example.com", "formats": ["markdown"]}).json()
    assert body["success"] is True
    assert body["data"]["markdown"] == "# Hi"
    assert body["data"]["json"] == {"x": 1}
    assert body["data"]["metadata"]["title"] == "Example"
    assert body["data"]["cost_class"] == "cheap"  # Aztea-native extra passed through


def test_scrape_error_envelope_maps_to_success_false(monkeypatch):
    monkeypatch.setenv("AZTEA_WEB_API_ENABLED", "1")
    monkeypatch.setattr(
        web_api._site_navigator, "run",
        lambda payload: {"error": {"code": "site_navigator.url_blocked", "message": "no"}},
    )
    body = _client().post("/scrape", json={"url": "http://localhost"}).json()
    assert body["success"] is False and body["error"]["code"] == "site_navigator.url_blocked"


def test_map_returns_links(monkeypatch):
    monkeypatch.setenv("AZTEA_WEB_API_ENABLED", "1")
    monkeypatch.setattr(
        web_api._sitemap, "map_site",
        lambda url, limit=2000: {"urls": ["https://example.com/a"], "count": 1},
    )
    body = _client().post("/map", json={"url": "https://example.com"}).json()
    assert body == {"success": True, "links": ["https://example.com/a"], "count": 1}


def _follow_links_fixtures(monkeypatch):
    """Wire a feed with two content links: one static article (HTTP succeeds), one
    JS/blocked article (HTTP fails -> bounded render fallback rescues it)."""
    from agents import _site_fetch

    feed_html = (
        '<a href="https://blog.example.org/long-static-article-title-here">A long static article title here</a>'
        '<a href="https://spa.example.org/app-article">Short SPA title yes</a>'
    )
    article_html = "<html><title>Static</title><body><p>" + ("Readable words here. " * 40) + "</p></body></html>"

    def nav_run(payload):
        if payload["url"].startswith("https://spa.example.org/"):
            return {"markdown": "Rendered article body. " * 40, "site_map": {"title": "Rendered"}}
        return {"url": "https://feed.example.org/", "requested_url": payload["url"],
                "result": None, "markdown": "# Feed", "html": feed_html,
                "site_map": {"title": "Feed"}, "source": "http_first", "cost_class": "cheap"}

    def fetch(url):
        if url.startswith("https://blog.example.org/"):
            return _site_fetch.FetchResult(final_url=url, status=200, content_type="text/html", html=article_html)
        return None  # the SPA article's static fetch dies

    monkeypatch.setenv("AZTEA_WEB_API_ENABLED", "1")
    monkeypatch.setattr(web_api._site_navigator, "run", nav_run)
    monkeypatch.setattr(web_api._site_fetch, "fetch_static_html", fetch)


def test_scrape_follow_links_keeps_failed_links_and_renders_thin_ones(monkeypatch):
    _follow_links_fixtures(monkeypatch)
    body = _client().post(
        "/scrape", json={"url": "https://feed.example.org", "formats": ["markdown"], "follow_links": 5},
    ).json()
    pages = body["data"]["linked_pages"]
    # Both requested links are present, document order kept — the dead static fetch
    # did NOT silently drop the second story; the render fallback filled it in.
    assert [p["via"] for p in pages] == ["http", "render"]
    assert pages[0]["chars"] > 400 and "Readable words" in pages[0]["markdown"]
    assert pages[1]["title"] == "Rendered" and "Rendered article body" in pages[1]["markdown"]
    assert all("needs_render" not in p for p in pages)  # internal marker never leaks
    assert "html" not in body["data"]  # fetched internally for link discovery only


def test_scrape_metadata_status_code_reflects_http_first_status(monkeypatch):
    monkeypatch.setenv("AZTEA_WEB_API_ENABLED", "1")
    monkeypatch.setattr(web_api._site_navigator, "run", lambda payload: {
        "url": "https://example.com", "requested_url": "https://example.com",
        "result": None, "markdown": "# Hi", "site_map": {"title": "Example"},
        "source": "http_first", "cost_class": "cheap", "http_status": 203,
    })
    body = _client().post("/scrape", json={"url": "https://example.com"}).json()
    assert body["data"]["metadata"]["statusCode"] == 203


def test_web_verify_validates_a_signed_receipt(monkeypatch):
    # End-to-end provenance: sign a receipt object, POST it, get valid:true — no DB,
    # no re-crawl. This is the "verify without trusting us" differentiator.
    priv, pub = crypto.generate_signing_keypair()
    _or = web_api.observation_receipts
    observation = {
        "request_url": "https://x", "final_url": "https://x", "http_status": None,
        "content_type": None, "snapshot_kind": "accessibility_tree",
        "dom_sha256": "d", "dom_bytes": 3,
        "extraction_sha256": _or._sha256_hex(crypto.canonical_json({"a": 1})),
    }
    sigil = _or.build_signing_payload(
        receipt_id="r1", job_id="", agent_id="agent-x", signer_kind="agent",
        observed_at=123, observation=observation,
    )
    receipt = {
        "receipt_id": "r1", "job_id": "", "agent_id": "agent-x", "signer_kind": "agent",
        "signer_did": "did:web:x", "observed_at": 123, "observation": observation,
        "extraction": {"a": 1}, "signature": crypto.sign_payload(priv, sigil),
    }
    monkeypatch.setattr(_or, "_resolve_public_pem", lambda aid: pub)
    body = _client().post("/web/verify", json={"receipt": receipt}).json()
    assert body["valid"] is True and body["checks"]["signature_ok"] is True
    # Tampering the extraction breaks the hash check.
    bad = _client().post("/web/verify", json={"receipt": dict(receipt, extraction={"a": 2})}).json()
    assert bad["valid"] is False
