"""Unit tests for core.site_maps.api_discovery (pure logic + SSRF-gated replay).

The security-critical assertions: param substitution can never alter the signed
endpoint authority (host-injection refusal), cross-origin reuse is blocked by the
registrable-domain gate, and replay refuses non-public targets without a network
round-trip.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from core.site_maps import api_discovery as ad


# --------------------------------------------------------------------------- domain provenance
def test_same_registrable_domain_allows_subdomains_blocks_cross_origin():
    assert ad.same_registrable_domain("api.example.com", "www.example.com") is True
    assert ad.same_registrable_domain("example.com", "example.com") is True
    assert ad.same_registrable_domain("api.bank.com", "attacker.com") is False
    assert ad.same_registrable_domain("", "example.com") is False


def test_registrable_domain_handles_multi_label_tld():
    assert ad.registrable_domain("api.shop.co.uk") == "shop.co.uk"
    assert ad.same_registrable_domain("api.shop.co.uk", "www.shop.co.uk") is True
    # A bare two-label co.uk would otherwise look like the registrable domain;
    # the multi-label gate keeps shop.co.uk and evil.co.uk distinct.
    assert ad.same_registrable_domain("a.shop.co.uk", "b.evil.co.uk") is False


# --------------------------------------------------------------------------- split / reconstruct
def test_split_endpoint_separates_authority_and_path():
    parts = ad.split_endpoint("https://api.example.com:8443/v1/items?page=1")
    assert parts.scheme == "https" and parts.host == "api.example.com"
    assert parts.port == 8443 and parts.path == "/v1/items" and parts.query == "page=1"


def test_split_endpoint_rejects_non_http():
    with pytest.raises(ValueError):
        ad.split_endpoint("ftp://example.com/x")
    with pytest.raises(ValueError):
        ad.split_endpoint("not-a-url")


def test_reconstruct_endpoint_uses_signed_authority():
    spec = {"endpoint_scheme": "https", "endpoint_host": "api.example.com",
            "endpoint_port": None, "path_template": "/users/{id}", "query_template": ""}
    assert ad.reconstruct_endpoint(spec, {"id": "42"}) == "https://api.example.com/users/42"
    spec_port = dict(spec, endpoint_port=8443, path_template="/v1/items", query_template="page={p}")
    assert ad.reconstruct_endpoint(spec_port, {"p": "2"}) == "https://api.example.com:8443/v1/items?page=2"


def test_param_substitution_refuses_host_injection():
    # The core SSRF property: a param value can never escape the path/query into a
    # new authority. The signed host wins, always.
    spec = {"endpoint_scheme": "https", "endpoint_host": "api.example.com",
            "endpoint_port": None, "path_template": "/users/{id}", "query_template": ""}
    for evil in ["@evil.com", "evil.com", "evil.com/?x=", "@evil.com/x", "../../@evil.com", "evil.com:80"]:
        url = ad.reconstruct_endpoint(spec, {"id": evil})
        assert urlparse(url).hostname == "api.example.com", f"host escaped via {evil!r} -> {url}"

    # A query value cannot inject extra params or a fragment (', & = #' are encoded).
    qspec = {"endpoint_scheme": "https", "endpoint_host": "api.example.com",
             "endpoint_port": None, "path_template": "/search", "query_template": "q={q}"}
    url = ad.reconstruct_endpoint(qspec, {"q": "a&admin=1#frag"})
    assert urlparse(url).hostname == "api.example.com"
    assert list(parse_qs(urlparse(url).query).keys()) == ["q"]  # no injected 'admin' key
    assert urlparse(url).fragment == ""


def test_reconstruct_missing_param_raises():
    spec = {"endpoint_scheme": "https", "endpoint_host": "api.example.com",
            "endpoint_port": None, "path_template": "/users/{id}", "query_template": ""}
    with pytest.raises(ValueError):
        ad.reconstruct_endpoint(spec, {})  # {id} unsatisfied


def test_reconstruct_rejects_specs_without_signed_authority():
    with pytest.raises(ValueError):
        ad.reconstruct_endpoint({"endpoint_scheme": "https", "endpoint_host": "",
                                 "path_template": "/x", "query_template": ""})


# --------------------------------------------------------------------------- candidate selection
def _cap(url, body, method="GET"):
    return {"method": method, "url": url, "json": body}


def test_select_candidate_prefers_get_json_with_goal_overlap():
    caps = [
        _cap("https://api.example.com/telemetry", {"ok": 1}),
        _cap("https://api.example.com/pricing/tiers", {"tiers": [{"name": "Pro"}]}),
        _cap("https://api.example.com/pricing", {"x": 1}, method="POST"),  # POST skipped
    ]
    best = ad.select_candidate(caps, goal="extract the pricing tiers")
    assert best is not None and best["url"].endswith("/pricing/tiers")


def test_select_candidate_none_when_no_usable_json():
    assert ad.select_candidate([], goal="anything") is None
    assert ad.select_candidate([_cap("https://x/y", "plain text")], goal="g") is None
    assert ad.select_candidate([_cap("https://x/y", {}, method="POST")], goal="g") is None


# --------------------------------------------------------------------------- replay SSRF gate
def test_replay_blocks_non_public_targets_without_network():
    # IP literals trip the SSRF check directly (no DNS, no socket) -> None, fast.
    assert ad.replay("http://169.254.169.254/latest/meta-data/") is None  # cloud metadata
    assert ad.replay("http://127.0.0.1:8000/admin") is None               # loopback
    assert ad.replay("http://[::1]/x") is None                            # ipv6 loopback
