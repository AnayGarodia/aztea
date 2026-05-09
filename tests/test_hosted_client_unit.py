# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `core/hosted_client.py` — A1–A10 from the audit plan.

Goal: prove the OSS-mode promise (no outbound calls when disabled) and that
hosted-mode itself fails safely on every error path. These tests do not
require a DB or the FastAPI app; they monkey-patch `requests.post` /
`requests.get` directly.
"""

from __future__ import annotations

import logging
import os

import pytest
import requests

# Strip any inherited hosted env BEFORE importing hosted_client.
for _v in ("AZTEA_HOSTED_API_URL", "AZTEA_HOSTED_API_KEY", "ALLOW_PRIVATE_OUTBOUND_URLS"):
    os.environ.pop(_v, None)

from core import hosted_client  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_global_client():
    hosted_client.reset_hosted_client_for_tests()
    yield
    hosted_client.reset_hosted_client_for_tests()


# ---------------------------------------------------------------------------
# A1. SSRF / target validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://10.0.0.1",
        "http://[::1]:80",
        "http://localhost:9000",
    ],
)
def test_ssrf_private_targets_rejected(url, monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_OUTBOUND_URLS", raising=False)
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", url)
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k")
    hosted_client.reset_hosted_client_for_tests()

    fired = {"called": False}

    def _post(*args, **kwargs):
        fired["called"] = True
        raise AssertionError("SSRF: outbound request fired for blocked URL")

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    monkeypatch.setattr(hosted_client.requests, "get", _post)

    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({"x": 1}) is None
    assert client.fetch_trust("did:web:host:agents:abc") is None
    assert fired["called"] is False


def test_ssrf_non_http_scheme_rejected(monkeypatch):
    """A non-http(s) base URL must be rejected by url_security."""
    monkeypatch.delenv("ALLOW_PRIVATE_OUTBOUND_URLS", raising=False)
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "file:///etc/passwd")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k")
    hosted_client.reset_hosted_client_for_tests()

    def _explode(*a, **kw):
        raise AssertionError("Should not have fired")

    monkeypatch.setattr(hosted_client.requests, "post", _explode)
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({}) is None


# ---------------------------------------------------------------------------
# A2. Response size cap
# ---------------------------------------------------------------------------


class _Resp:
    """Tiny faux requests.Response with ok/headers/url/iter_content/__enter__."""

    def __init__(self, *, ok=True, status_code=200, headers=None, chunks=None, url="https://x/y"):
        self.ok = ok
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self._chunks = list(chunks or [])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, chunk_size=0):
        for c in self._chunks:
            yield c

    def close(self):
        # Required by _post_acknowledge (which does not stream).
        return None


def _enable(monkeypatch):
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "azh_token")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    hosted_client.reset_hosted_client_for_tests()


def test_response_oversized_streaming_aborts(monkeypatch):
    _enable(monkeypatch)
    big = b"x" * (hosted_client._MAX_RESPONSE_BYTES // 2 + 1)

    def _post(url, **kw):
        # Two chunks, each over half the cap → total > cap; must abort.
        return _Resp(chunks=[big, big])

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({"x": 1}) is None


def test_response_declared_content_length_too_large(monkeypatch):
    _enable(monkeypatch)
    too_big = str(hosted_client._MAX_RESPONSE_BYTES + 1)

    def _post(url, **kw):
        return _Resp(headers={"Content-Length": too_big}, chunks=[b'{"verdict":"agent_wins"}'])

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({"x": 1}) is None


def test_response_just_under_cap_succeeds(monkeypatch):
    _enable(monkeypatch)
    body = b'{"verdict":"agent_wins","reasoning":"r","confidence":0.5}'

    def _post(url, **kw):
        return _Resp(chunks=[body])

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    client = hosted_client.get_hosted_client()
    out = client.judge_dispute({"x": 1})
    assert out and out["verdict"] == "agent_wins"


# ---------------------------------------------------------------------------
# A3. Malformed JSON handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xfe\xfd not utf-8",
        b"[1,2,3]",
        b"",
        b"not json at all",
    ],
)
def test_malformed_json_returns_none(monkeypatch, body):
    _enable(monkeypatch)
    monkeypatch.setattr(
        hosted_client.requests, "post", lambda url, **kw: _Resp(chunks=[body])
    )
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({"x": 1}) is None


def test_minimal_dict_response_passes(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        hosted_client.requests,
        "post",
        lambda url, **kw: _Resp(chunks=[b'{"verdict":"agent_wins"}']),
    )
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({}) == {"verdict": "agent_wins"}


# ---------------------------------------------------------------------------
# A4. No redirect following
# ---------------------------------------------------------------------------


def test_no_redirect_following(monkeypatch):
    """allow_redirects=False is explicitly set; verify we pass it through."""
    _enable(monkeypatch)
    captured = {}

    def _post(url, **kw):
        captured.update(kw)
        return _Resp(ok=False, status_code=302, headers={"Location": "http://10.0.0.1/x"})

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({}) is None
    assert captured.get("allow_redirects") is False


# ---------------------------------------------------------------------------
# A5. Timeouts and connection errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls",
    [requests.Timeout, requests.ConnectionError, requests.RequestException],
)
def test_request_exceptions_return_none(monkeypatch, exc_cls):
    _enable(monkeypatch)

    def _raise(*a, **kw):
        raise exc_cls("boom")

    monkeypatch.setattr(hosted_client.requests, "post", _raise)
    monkeypatch.setattr(hosted_client.requests, "get", _raise)
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({}) is None
    assert client.call_agent("x", {}) is None
    assert client.publish_listing({}) is None
    assert client.push_rating({}) is False
    assert client.fetch_trust("did:web:host:agents:abc") is None


# ---------------------------------------------------------------------------
# A6. API key never logged
# ---------------------------------------------------------------------------


def test_bearer_token_never_logged(monkeypatch, caplog):
    _enable(monkeypatch)

    def _post(url, **kw):
        return _Resp(ok=False, status_code=502, chunks=[b"upstream sent: Bearer azh_token leaked"])

    monkeypatch.setattr(hosted_client.requests, "post", _post)
    caplog.set_level(logging.DEBUG, logger="core.hosted_client")
    client = hosted_client.get_hosted_client()
    assert client.judge_dispute({}) is None
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "Bearer azh_token" not in msg
        assert "azh_token" not in msg


# ---------------------------------------------------------------------------
# A7. Empty-string env handling
# ---------------------------------------------------------------------------


def test_empty_string_env_disables(monkeypatch):
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "")
    hosted_client.reset_hosted_client_for_tests()
    client = hosted_client.get_hosted_client()
    assert client.is_enabled() is False

    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "")
    hosted_client.reset_hosted_client_for_tests()
    client = hosted_client.get_hosted_client()
    assert client.is_enabled() is False


# ---------------------------------------------------------------------------
# A8. Cache invalidation on env change
# ---------------------------------------------------------------------------


def test_cache_invalidates_on_env_change(monkeypatch):
    monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    monkeypatch.delenv("AZTEA_HOSTED_API_KEY", raising=False)
    hosted_client.reset_hosted_client_for_tests()
    c1 = hosted_client.get_hosted_client()
    assert c1.is_enabled() is False

    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k1")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    c2 = hosted_client.get_hosted_client()
    assert c2 is not c1
    assert c2.is_enabled() is True

    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k2")
    c3 = hosted_client.get_hosted_client()
    assert c3 is not c2
    assert c3._api_key == "k2"

    monkeypatch.delenv("AZTEA_HOSTED_API_URL", raising=False)
    c4 = hosted_client.get_hosted_client()
    assert c4 is not c3
    assert c4.is_enabled() is False


# ---------------------------------------------------------------------------
# A9. fetch_trust DID handling — the "URL-encode the DID" probe
# ---------------------------------------------------------------------------


def test_fetch_trust_url_includes_did(monkeypatch):
    """L1 / A9: capture the URL handed to requests.get when DID has colons.

    Today, hosted_client builds f"/v1/trust/{agent_did}" with NO escaping.
    The colons in did:web:host%3A8000:agents:uuid go into the URL path
    verbatim. RFC 3986 allows colons in path segments, so this is technically
    OK, but URL-encoded substrings (%3A) inside the DID must NOT be
    double-encoded by url_security, and the request must actually fire.
    """
    monkeypatch.setenv("AZTEA_HOSTED_API_URL", "https://api.aztea.test")
    monkeypatch.setenv("AZTEA_HOSTED_API_KEY", "k")
    monkeypatch.setenv("ALLOW_PRIVATE_OUTBOUND_URLS", "1")
    hosted_client.reset_hosted_client_for_tests()

    captured = {}

    def _get(url, **kw):
        captured["url"] = url
        return _Resp(chunks=[b'{"trust_score": 88.0}'])

    monkeypatch.setattr(hosted_client.requests, "get", _get)
    client = hosted_client.get_hosted_client()

    did = "did:web:localhost%3A8000:agents:abc-uuid"
    out = client.fetch_trust(did)
    assert out == {"trust_score": 88.0}
    # The DID must appear in the URL; the request must have been made with
    # something a hosted server can route. Today this is raw concatenation.
    assert "abc-uuid" in captured["url"]
    assert "localhost" in captured["url"]
    # If url_security ever doubled the encoding (%253A), the test fails.
    assert "%253A" not in captured["url"]


def test_fetch_trust_empty_did_short_circuits(monkeypatch):
    _enable(monkeypatch)

    def _get(url, **kw):
        raise AssertionError("must not call get for empty DID")

    monkeypatch.setattr(hosted_client.requests, "get", _get)
    client = hosted_client.get_hosted_client()
    assert client.fetch_trust("") is None


# ---------------------------------------------------------------------------
# A10. push_rating on a 204-style empty body
# ---------------------------------------------------------------------------


def test_push_rating_returns_true_on_empty_body(monkeypatch):
    """A 2xx with an empty body (e.g. 204 No Content) is a legitimate
    fire-and-forget acknowledgement. push_rating must report True so callers
    don't log spurious "push failed" warnings on a working hosted endpoint.

    This was a documented finding pre-fix; the behavior is now corrected via
    `_post_acknowledge` which checks `response.ok` and ignores the body.
    """
    _enable(monkeypatch)
    monkeypatch.setattr(
        hosted_client.requests,
        "post",
        lambda url, **kw: _Resp(chunks=[], status_code=204),
    )
    client = hosted_client.get_hosted_client()
    assert client.push_rating({"job_id": "j", "rating": 5}) is True


def test_push_rating_returns_true_on_minimal_dict(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(
        hosted_client.requests,
        "post",
        lambda url, **kw: _Resp(chunks=[b"{}"]),
    )
    client = hosted_client.get_hosted_client()
    assert client.push_rating({"job_id": "j"}) is True


def test_push_rating_returns_false_on_5xx(monkeypatch):
    """A real hosted error (5xx) must still report False so the daemon
    thread logs the failure at debug level and we have a signal for the
    circuit breaker."""
    _enable(monkeypatch)
    monkeypatch.setattr(
        hosted_client.requests,
        "post",
        lambda url, **kw: _Resp(ok=False, status_code=502, chunks=[]),
    )
    client = hosted_client.get_hosted_client()
    assert client.push_rating({"job_id": "j"}) is False


# ---------------------------------------------------------------------------
# Bonus: call_agent with non-string slug short-circuits
# ---------------------------------------------------------------------------


def test_call_agent_rejects_bad_slug(monkeypatch):
    _enable(monkeypatch)

    def _explode(*a, **kw):
        raise AssertionError("call_agent should short-circuit on bad slug")

    monkeypatch.setattr(hosted_client.requests, "post", _explode)
    client = hosted_client.get_hosted_client()
    assert client.call_agent("", {}) is None
    assert client.call_agent(None, {}) is None  # type: ignore[arg-type]
    assert client.call_agent(123, {}) is None  # type: ignore[arg-type]
