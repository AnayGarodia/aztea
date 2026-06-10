"""Unit tests for the pluggable fetch backend (Phase C): env selection + httpx kwargs.

Off-by-default is the load-bearing property: with no env set, backend is 'direct' and
httpx_kwargs() is empty, so the fetch path is byte-identical to before Phase C.
"""

from __future__ import annotations

from core.web import fetch_backend as fb


def test_backend_defaults_to_direct_no_proxy(monkeypatch):
    monkeypatch.delenv("AZTEA_FETCH_BACKEND", raising=False)
    monkeypatch.delenv("AZTEA_FETCH_PROXY_URL", raising=False)
    assert fb.backend_name() == "direct"
    assert fb.proxy_url() is None
    assert fb.httpx_kwargs() == {}          # off path adds nothing to httpx.Client
    assert fb.remote_browser_config() is None


def test_proxy_url_flips_httpx_kwargs(monkeypatch):
    monkeypatch.setenv("AZTEA_FETCH_PROXY_URL", "http://user:pass@proxy.example:8080")
    assert fb.proxy_url() == "http://user:pass@proxy.example:8080"
    assert fb.httpx_kwargs() == {"proxy": "http://user:pass@proxy.example:8080"}


def test_remote_browser_config_only_when_selected(monkeypatch):
    monkeypatch.setenv("AZTEA_FETCH_BACKEND", "remote_browser")
    monkeypatch.setenv("AZTEA_REMOTE_BROWSER_API_KEY", "bb_key")
    cfg = fb.remote_browser_config()
    assert cfg is not None and cfg["provider"] == "browserbase" and cfg["api_key"] == "bb_key"
    monkeypatch.setenv("AZTEA_FETCH_BACKEND", "direct")
    assert fb.remote_browser_config() is None
