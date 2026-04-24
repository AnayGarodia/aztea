"""Integration tests for github_fetcher, hn_digest, and dns_inspector built-in agents."""

from unittest.mock import patch, MagicMock

from tests.integration.support import *  # noqa: F403

GITHUB_FETCHER_AGENT_ID = server._GITHUB_FETCHER_AGENT_ID
HN_DIGEST_AGENT_ID = server._HN_DIGEST_AGENT_ID
DNS_INSPECTOR_AGENT_ID = server._DNS_INSPECTOR_AGENT_ID


def test_github_fetcher_basic(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "# Hello World\nThis is a README."
    mock_resp.content = b"# Hello World\nThis is a README."

    with patch("agents.github_fetcher.httpx.get", return_value=mock_resp):
        resp = client.post(
            f"/registry/agents/{GITHUB_FETCHER_AGENT_ID}/call",
            json={"repo": "octocat/Hello-World", "paths": ["README.md"]},
            headers=_auth_headers(caller["raw_api_key"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "files" in body
    assert isinstance(body["billing_units_actual"], int)
    assert body["billing_units_actual"] == 1
    assert body["files"][0]["path"] == "README.md"
    assert body["files"][0]["content"] == "# Hello World\nThis is a README."


def test_hn_digest_basic(client):
    caller = _register_user()
    _fund_user_wallet(caller, 200)

    mock_hn_resp = MagicMock()
    mock_hn_resp.status_code = 200
    mock_hn_resp.raise_for_status = MagicMock()
    mock_hn_resp.json.return_value = {
        "hits": [
            {
                "title": "Show HN: New AI tool",
                "url": "https://example.com",
                "points": 150,
                "num_comments": 45,
                "author": "testuser",
                "created_at": "2024-01-01T00:00:00Z",
            },
            {
                "title": "Ask HN: Best practices for Python",
                "url": None,
                "points": 200,
                "num_comments": 80,
                "author": "devuser",
                "created_at": "2024-01-01T01:00:00Z",
            },
        ]
    }

    mock_llm = MagicMock()
    mock_llm.text = "SYNTHESIS: AI and developer tools dominate today.\nTOPICS: AI, Python, tools"

    with patch("agents.hn_digest.httpx.get", return_value=mock_hn_resp), \
         patch("agents.hn_digest.run_with_fallback", return_value=mock_llm):
        resp = client.post(
            f"/registry/agents/{HN_DIGEST_AGENT_ID}/call",
            json={"count": 2, "mode": "digest"},
            headers=_auth_headers(caller["raw_api_key"]),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "stories" in body
    assert "synthesis" in body
    assert isinstance(body["billing_units_actual"], int)
    assert body["billing_units_actual"] == 2
    assert len(body["stories"]) == 2
    assert body["stories"][0]["title"] == "Show HN: New AI tool"


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
    body = resp.json()
    assert "results" in body
    assert len(body["results"]) == 1
    assert body["results"][0]["domain"] == "example.com"
    assert body["results"][0]["a_records"] == ["93.184.216.34"]
    assert isinstance(body["billing_units_actual"], int)
    assert body["billing_units_actual"] == 1
