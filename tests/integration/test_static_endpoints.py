"""Integration tests for /robots.txt and /.well-known/security.txt.

Audit 2026-05-16 #16 + #17: both endpoints previously fell through to the
SPA catch-all (HTML for robots, 404 for security.txt).
"""

from tests.integration.support import *  # noqa: F403


def test_robots_txt_served_as_plain_text(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    assert "User-agent: *" in body
    assert "Disallow:" in body


def test_security_txt_served_per_rfc_9116(client):
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # Contact + canonical URLs are env-derived (SECURITY_TXT_CONTACT,
    # SERVER_BASE_URL) so the OSS build can self-host without a hardcoded
    # aztea.ai reference (audit checker enforces this in core/server/agents).
    assert "Contact: " in body
    assert "Expires:" in body
    assert "Canonical: " in body
    assert "/.well-known/security.txt" in body


def test_sitemap_xml_served_as_xml(client):
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/xml")
    assert "<urlset" in resp.text
    assert "/agents</loc>" in resp.text


def test_legacy_openapi_json_returns_spec(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["openapi"].startswith("3.")
