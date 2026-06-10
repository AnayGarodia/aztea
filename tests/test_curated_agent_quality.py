"""Tests for the 2026-06-10 curated-agent quality improvements.

Split from tests/test_agent_real_tool.py to respect the 1000-line file
budget. Covers: dependency_auditor manifest parsing + SPDX, cve_lookup
KEV/range/dedup, dns_inspector record checks, db_sandbox transactions +
hints, accessibility_auditor vendored-axe fallback, browser_agent
truncation/network-filter signals, lighthouse_auditor throttling +
chrome-path resolution.
"""

from __future__ import annotations

import pytest

from agents import browser_agent
from agents import cve_lookup
from agents import db_sandbox
from agents import dependency_auditor


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return dict(self._payload)


def test_dependency_auditor_prerelease_version_reaches_osv_intact(monkeypatch):
    """Regression: the old npm parser digit-stripped ^1.2.3-beta.1 into
    1.2.3.1, so OSV was queried for a version that never existed."""
    seen_versions: list[str] = []

    def fake_post(url, json=None, timeout=None, headers=None):
        del url, timeout, headers
        seen_versions.append(json["version"])
        return _FakeResponse(200, {"vulns": []})

    def fake_get(url, timeout=None, headers=None):
        del url, timeout, headers
        return _FakeResponse(200, {"dist-tags": {"latest": "1.2.3"}, "versions": {}})

    monkeypatch.setattr(dependency_auditor.requests, "post", fake_post)
    monkeypatch.setattr(dependency_auditor.requests, "get", fake_get)

    result = dependency_auditor.run(
        {
            "manifest": '{"dependencies": {"express": "^1.2.3-beta.1"}}',
            "checks": ["cve"],
        }
    )
    assert "error" not in result, result
    assert seen_versions == ["1.2.3-beta.1"]


def test_dependency_auditor_classifies_vcs_and_editable_warnings(monkeypatch):
    """Editable installs and VCS URLs must surface as classified warnings,
    not generic 'unparseable' noise."""
    monkeypatch.setattr(
        dependency_auditor.requests,
        "post",
        lambda *a, **k: _FakeResponse(200, {"vulns": []}),
    )
    result = dependency_auditor.run(
        {
            "manifest": (
                "requests==2.31.0\n"
                "-e git+https://github.com/x/y.git#egg=y\n"
                "git+https://github.com/a/b.git@v2#egg=b\n"
            ),
            "checks": ["cve"],
        }
    )
    assert "error" not in result, result
    reasons = sorted(w["reason"] for w in result["parse_warnings"])
    assert reasons == ["editable_not_audited", "vcs_url_not_audited"]


def test_dependency_auditor_spdx_or_expression_takes_permissive_branch():
    assert dependency_auditor._license_risk("MIT OR GPL-2.0") == "none"
    assert dependency_auditor._license_risk("MIT AND GPL-2.0") == "high"
    assert dependency_auditor._license_risk("LGPL-2.1-only") == "medium"
    assert dependency_auditor._license_risk("MPL-2.0") == "medium"
    assert dependency_auditor._license_risk("AGPL-3.0") == "high"


def _osv_vuln_payload(cve_id="CVE-2024-99999", introduced="2.0", fixed="4.17.21"):
    return {
        "vulns": [
            {
                "id": "GHSA-kev1",
                "aliases": [cve_id],
                "summary": "Some memory corruption issue",
                "published": "2024-01-02T00:00:00.000",
                "modified": "2024-01-03T00:00:00.000",
                "severity": [{"score": "8.8"}],
                "affected": [
                    {
                        "ranges": [
                            {
                                "type": "SEMVER",
                                "events": [
                                    {"introduced": introduced},
                                    {"fixed": fixed},
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def _nvd_cve_payload(cve_id="CVE-2024-99999"):
    return {
        "vulnerabilities": [
            {
                "cve": {
                    "id": cve_id,
                    "published": "2024-01-02T00:00:00.000",
                    "lastModified": "2024-01-03T00:00:00.000",
                    "descriptions": [
                        {"lang": "en", "value": "Some memory corruption issue"}
                    ],
                    "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.1}}]},
                    "references": [],
                }
            }
        ]
    }


def _patch_cve_network(monkeypatch, cve_id="CVE-2024-99999", **osv_kwargs):
    monkeypatch.setattr(cve_lookup, "_NVD_RATE_DELAY", 0)
    cve_lookup._cve_cache.clear()
    monkeypatch.setattr(
        cve_lookup.requests,
        "post",
        lambda *a, **k: _FakeResponse(200, _osv_vuln_payload(cve_id, **osv_kwargs)),
    )
    monkeypatch.setattr(
        cve_lookup.requests,
        "get",
        lambda *a, **k: _FakeResponse(200, _nvd_cve_payload(cve_id)),
    )


def test_cve_lookup_kev_listing_flips_exploit_available(monkeypatch):
    """A CVE on the CISA KEV catalog must report exploit_available=True with
    exploit_source=cisa_kev even when its description has no exploit keyword."""
    from agents import _kev_feed

    _patch_cve_network(monkeypatch)
    monkeypatch.setattr(
        _kev_feed,
        "_fetch_catalog",
        lambda: {"CVE-2024-99999": {"date_added": "2024-02-01", "ransomware": False}},
    )
    _kev_feed.reset_cache()

    result = cve_lookup.run({"packages": ["lodash@4.17.20"]})
    row = result["results"][0]
    assert row["exploit_available"] is True
    assert row["exploit_source"] == "cisa_kev"
    assert row["kev"] == {"listed": True, "date_added": "2024-02-01"}
    assert result["exploit_intel_degraded"] is False


def test_cve_lookup_kev_outage_degrades_to_keyword_heuristic(monkeypatch):
    """KEV feed down: the call still succeeds, exploit_intel_degraded=True,
    and a description containing 'exploit' still flags via the heuristic."""
    _patch_cve_network(monkeypatch)
    # conftest's autouse fixture already stubs _fetch_catalog -> None (outage).

    result = cve_lookup.run({"packages": ["lodash@4.17.20"]})
    row = result["results"][0]
    assert result["exploit_intel_degraded"] is True
    assert row["exploit_source"] is None  # no exploit keyword in description
    assert row["kev"] == {"listed": False, "date_added": None}


def test_kev_feed_caches_catalog_across_calls(monkeypatch):
    from agents import _kev_feed

    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return {"CVE-2024-1": {"date_added": "2024-01-01", "ransomware": False}}

    monkeypatch.setattr(_kev_feed, "_fetch_catalog", fake_fetch)
    _kev_feed.reset_cache()
    assert _kev_feed.kev_entries(["CVE-2024-1"]) == {
        "CVE-2024-1": {"date_added": "2024-01-01", "ransomware": False}
    }
    assert _kev_feed.kev_entries(["CVE-2024-2"]) == {}
    assert calls["n"] == 1, "second call must be served from the TTL cache"


def test_cve_lookup_range_filter_excludes_version_below_introduced(monkeypatch):
    """Regression: '< fixed' ranges marked versions BELOW the introduced
    bound as vulnerable. lodash 1.0.0 with an advisory on >=2.0,<4.17.21
    must NOT be reported."""
    _patch_cve_network(monkeypatch, introduced="2.0", fixed="4.17.21")

    affected = cve_lookup.run({"packages": ["lodash@3.0.0"]})
    assert len(affected["results"]) == 1

    cve_lookup._cve_cache.clear()
    safe_below = cve_lookup.run({"packages": ["lodash@1.0.0"]})
    assert safe_below["results"] == []

    cve_lookup._cve_cache.clear()
    safe_above = cve_lookup.run({"packages": ["lodash@4.17.21"]})
    assert safe_above["results"] == []


def test_cve_lookup_duplicate_ids_deduped_and_billed_once(monkeypatch):
    _patch_cve_network(monkeypatch)

    result = cve_lookup.run({"cve_ids": ["CVE-2024-99999", "cve-2024-99999"]})
    assert len(result["results"]) == 1
    assert result["billing_units_actual"] == 1
    assert result["nvd_key_configured"] is False


class _FakeMXRecord:
    def __init__(self, exchange: str, preference: int):
        self.exchange = exchange
        self.preference = preference


class _FakeTXTRecord:
    def __init__(self, *strings: bytes):
        self.strings = strings


class NXDOMAIN(Exception):
    """Stand-in for dns.resolver.NXDOMAIN — class NAME is what the agent's
    _is_dns_absence classifier matches, so a missing record reads as a
    definitive absence (not a transport failure)."""


class _FakeResolver:
    """Minimal dns.resolver.Resolver stand-in keyed on (name, rtype)."""

    def __init__(self, table: dict):
        self._table = table

    def resolve(self, name, rtype):
        key = (name, rtype)
        if key not in self._table:
            raise NXDOMAIN(f"{key}")
        return self._table[key]


def test_dns_inspector_real_mx_lookup(monkeypatch):
    """MX must come from the real RRset — the old mail.<domain> heuristic
    missed every hosted-mail setup (Google Workspace -> aspmx.l.google.com)."""
    from agents import dns_inspector

    resolver = _FakeResolver(
        {
            ("example.com", "MX"): [
                _FakeMXRecord("alt1.aspmx.l.google.com.", 5),
                _FakeMXRecord("aspmx.l.google.com.", 1),
            ]
        }
    )
    monkeypatch.setattr(dns_inspector, "_dnspython_resolver", lambda: resolver)
    result = dns_inspector.run({"domains": ["example.com"], "checks": ["mx"]})
    entry = result["results"][0]
    assert entry["mx_method"] == "dns"
    assert entry["mx"] == [
        {"host": "aspmx.l.google.com", "priority": 1},
        {"host": "alt1.aspmx.l.google.com", "priority": 5},
    ]


def test_dns_inspector_mx_falls_back_to_heuristic_without_dnspython(monkeypatch):
    from agents import dns_inspector

    monkeypatch.setattr(dns_inspector, "_dnspython_resolver", lambda: None)
    monkeypatch.setattr(
        dns_inspector,
        "_cached_getaddrinfo",
        lambda host, family=None: [(2, 1, 6, "", ("1.2.3.4", 0))],
    )
    result = dns_inspector.run({"domains": ["example.com"], "checks": ["mx"]})
    entry = result["results"][0]
    assert entry["mx_method"] == "heuristic"
    assert entry["possible_mail_ips"] == ["1.2.3.4"]


def test_dns_inspector_spf_and_dmarc_checks(monkeypatch):
    from agents import dns_inspector

    resolver = _FakeResolver(
        {
            ("example.com", "TXT"): [
                _FakeTXTRecord(b"v=spf1 include:_spf.google.com ~all"),
                _FakeTXTRecord(b"google-site-verification=abc"),
            ],
            ("_dmarc.example.com", "TXT"): [
                _FakeTXTRecord(b"v=DMARC1; p=quarantine; rua=mailto:d@example.com"),
            ],
        }
    )
    monkeypatch.setattr(dns_inspector, "_dnspython_resolver", lambda: resolver)
    result = dns_inspector.run(
        {"domains": ["example.com"], "checks": ["txt", "dmarc"]}
    )
    entry = result["results"][0]
    assert entry["spf"] == "v=spf1 include:_spf.google.com ~all"
    assert entry["dmarc"]["present"] is True
    assert entry["dmarc"]["policy"] == "quarantine"


def test_dns_inspector_missing_spf_and_dmarc_raise_issues(monkeypatch):
    from agents import dns_inspector

    resolver = _FakeResolver({("example.com", "TXT"): [_FakeTXTRecord(b"other")]})
    monkeypatch.setattr(dns_inspector, "_dnspython_resolver", lambda: resolver)
    result = dns_inspector.run(
        {"domains": ["example.com"], "checks": ["txt", "dmarc"]}
    )
    entry = result["results"][0]
    assert entry["spf"] is None
    assert entry["dmarc"] == {"present": False, "policy": None}
    assert "No SPF record found" in entry["issues"]
    assert "No DMARC record found" in entry["issues"]


def test_dns_inspector_rejects_unknown_check():
    from agents import dns_inspector

    result = dns_inspector.run({"domains": ["example.com"], "checks": ["dnssec"]})
    assert result["error"]["code"] == "dns_inspector.unknown_check"


def test_dns_inspector_cert_expiry_threshold_configurable(monkeypatch):
    from agents import dns_inspector

    monkeypatch.setattr(
        dns_inspector,
        "_ssl_check",
        lambda domain: (
            {
                "issuer": {},
                "subject": {},
                "expires_at": "x",
                "days_until_expiry": 45,
                "san_names": [],
            },
            None,
        ),
    )
    default_run = dns_inspector.run({"domains": ["example.com"], "checks": ["ssl"]})
    assert default_run["results"][0]["issues"] == []

    strict_run = dns_inspector.run(
        {
            "domains": ["example.com"],
            "checks": ["ssl"],
            "cert_expiry_warn_days": 60,
        }
    )
    assert "SSL certificate expires in 45 days" in strict_run["results"][0]["issues"]

    bad = dns_inspector.run(
        {"domains": ["example.com"], "checks": ["ssl"], "cert_expiry_warn_days": 0}
    )
    assert bad["error"]["code"] == "dns_inspector.invalid_expiry_threshold"


def test_dns_inspector_http_security_headers_and_redirects(monkeypatch):
    from agents import dns_inspector

    class _FakeHeaders(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    class _FakeHTTPResponse:
        status = 200
        headers = _FakeHeaders(
            {
                "Server": "nginx",
                "Strict-Transport-Security": "max-age=63072000",
                "Content-Security-Policy": "default-src 'self'",
            }
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakeOpener:
        def open(self, req, timeout=None):
            return _FakeHTTPResponse()

    def fake_build_opener(handler):
        handler.chain.append({"status": 301, "to": "https://example.com/"})
        return _FakeOpener()

    monkeypatch.setattr(
        dns_inspector.urllib.request, "build_opener", fake_build_opener
    )
    info, err = dns_inspector._http_check("example.com")
    assert err is None
    assert info["hsts"] is True
    assert info["security_headers"] == {
        "Strict-Transport-Security": "max-age=63072000",
        "Content-Security-Policy": "default-src 'self'",
    }
    assert info["redirect_chain"] == [{"status": 301, "to": "https://example.com/"}]


def test_db_sandbox_supports_explicit_transactions():
    """BEGIN/ROLLBACK must behave atomically — the old per-statement
    auto-commit made caller-issued ROLLBACK silently meaningless."""
    result = db_sandbox.run(
        {
            "schema_sql": "CREATE TABLE t(id INTEGER);",
            "queries": [
                "BEGIN",
                "INSERT INTO t VALUES (1)",
                "ROLLBACK",
                "SELECT count(*) AS n FROM t",
            ],
        }
    )
    assert "error" not in result, result
    assert result["results"][3]["rows"][0]["n"] == 0
    assert result["warnings"] == []


def test_db_sandbox_commit_persists_within_call():
    result = db_sandbox.run(
        {
            "schema_sql": "CREATE TABLE t(id INTEGER);",
            "queries": [
                "BEGIN",
                "INSERT INTO t VALUES (1)",
                "COMMIT",
                "SELECT count(*) AS n FROM t",
            ],
        }
    )
    assert result["results"][3]["rows"][0]["n"] == 1


def test_db_sandbox_open_transaction_rolls_back_with_warning():
    result = db_sandbox.run(
        {
            "schema_sql": "CREATE TABLE t(id INTEGER);",
            "queries": ["BEGIN", "INSERT INTO t VALUES (1)"],
        }
    )
    assert "error" not in result, result
    assert any("rolled back" in w for w in result["warnings"]), result


def test_db_sandbox_suggests_index_for_full_scan():
    result = db_sandbox.run(
        {
            "schema_sql": (
                "CREATE TABLE items(id INTEGER PRIMARY KEY, color TEXT);"
                "INSERT INTO items(color) VALUES ('red'), ('blue');"
            ),
            "queries": [
                "SELECT * FROM items WHERE color = 'red'",
                "SELECT * FROM items WHERE id = 1",
            ],
        }
    )
    suggestions = result["index_suggestions"]
    assert any(
        s["table"] == "items" and s["statement_index"] == 0 for s in suggestions
    ), result
    # The id lookup uses the integer primary key — no suggestion for it.
    assert not any(s["statement_index"] == 1 for s in suggestions), suggestions


def test_db_sandbox_per_call_sql_budget():
    big = "SELECT 1 -- " + "x" * 39_000
    result = db_sandbox.run({"queries": [big, big, big]})
    assert result["error"]["code"] == "db_sandbox.sql_budget_exceeded"


def test_db_sandbox_results_truncated_flag():
    result = db_sandbox.run(
        {
            "schema_sql": (
                "CREATE TABLE n(x INTEGER);"
                "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq WHERE x < 600) "
                "INSERT INTO n SELECT x FROM seq;"
            ),
            "sql": "SELECT * FROM n",
        }
    )
    assert result["results"][0]["truncated"] is True
    assert result["results_truncated"] is True


def test_db_sandbox_invalid_payload_returns_envelope():
    result = db_sandbox.run(["not", "a", "dict"])
    assert result["error"]["code"] == "db_sandbox.invalid_payload"


def test_accessibility_vendored_axe_matches_cdn_version():
    """The vendored axe.min.js must stay in version-lockstep with
    _AXE_CDN_URL — drift would silently change rule behavior between the
    CDN path and the fallback path."""
    from agents import accessibility_auditor as aa
    import re as _re

    cdn_version = _re.search(r"/axe-core/([\d.]+)/", aa._AXE_CDN_URL).group(1)
    source = aa._vendored_axe_source()
    assert source, "vendored axe-core missing"
    assert f"axe v{cdn_version}" in source[:200], (
        f"vendored axe-core does not match CDN version {cdn_version}"
    )


def test_accessibility_inject_axe_falls_back_to_vendored():
    """CDN injection failure (outage / strict CSP) must fall back to the
    vendored copy instead of hard-failing the audit."""
    from agents import accessibility_auditor as aa

    class _FakePage:
        def __init__(self):
            self.inline_injected = False

        def add_script_tag(self, url=None, content=None):
            if url is not None:
                raise RuntimeError("net::ERR_BLOCKED_BY_CSP")
            assert content and "axe" in content[:100]
            self.inline_injected = True

    page = _FakePage()
    assert aa._inject_axe(page) == "vendored"
    assert page.inline_injected is True


def test_accessibility_inject_axe_errors_when_both_paths_fail(monkeypatch):
    from agents import accessibility_auditor as aa

    class _FakePage:
        def add_script_tag(self, url=None, content=None):
            raise RuntimeError("blocked")

    monkeypatch.setattr(aa, "_vendored_axe_cache", None)
    monkeypatch.setattr(aa, "_VENDORED_AXE_PATH", aa._VENDORED_AXE_PATH.parent / "nope.js")
    result = aa._inject_axe(_FakePage())
    assert result["error"]["code"] == "accessibility_auditor.axe_load_failed"


def test_accessibility_unknown_tags_advisory():
    from agents import accessibility_auditor as aa

    tags, unknown = aa._normalize_tags(["wcag21aa", "WCAG2.1-AA!"])
    assert tags == ["wcag21aa", "WCAG2.1-AA!"]  # passed through to axe
    assert unknown == ["WCAG2.1-AA!"]
    default_tags, default_unknown = aa._normalize_tags(None)
    assert default_tags == list(aa._DEFAULT_TAGS)
    assert default_unknown == []


def test_accessibility_response_reports_incomplete_and_truncation():
    from agents import accessibility_auditor as aa

    axe_result = {
        "testEngine": {"name": "axe-core", "version": "4.8.4"},
        "violations": [
            {"id": f"rule-{i}", "impact": "minor", "nodes": []} for i in range(35)
        ],
        "passes": [],
        "incomplete": [
            {
                "id": "color-contrast",
                "impact": "serious",
                "help": "Needs manual review",
                "helpUrl": "https://example.com/rule",
                "nodes": [{}, {}],
            }
        ],
    }
    response = aa._build_response(
        url="https://example.com",
        final_url="https://example.com/",
        page_title="t",
        axe_result=axe_result,
        elapsed_ms=10,
        axe_source="vendored",
        unknown_tags=[],
    )
    assert response["violations_truncated"] is True
    assert len(response["violations"]) == aa._MAX_VIOLATIONS
    assert response["totals"]["violations"] == 35
    assert response["axe_source"] == "vendored"
    assert response["incomplete"] == [
        {
            "id": "color-contrast",
            "impact": "serious",
            "help": "Needs manual review",
            "help_url": "https://example.com/rule",
            "node_count": 2,
        }
    ]


def test_browser_agent_rejects_unknown_network_capture_types():
    """A typo like 'ajax' must error, not silently capture nothing of what
    the caller wanted."""
    result = browser_agent.run(
        {
            "url": "https://example.com",
            "capture_network": True,
            "network_capture_types": ["ajax"],
        }
    )
    assert result["error"]["code"] == "browser_agent.invalid_network_types"


def test_browser_agent_network_type_filter_applied():
    from types import SimpleNamespace as _NS

    network_log: list = []
    handlers = {}

    class _FakePage:
        def on(self, event, handler):
            handlers[event] = handler

    browser_agent._attach_listeners(
        _FakePage(), network_log, [],
        capture_network=True, network_types=frozenset({"xhr"}),
    )
    xhr = _NS(
        url="https://api.example.com/data", status=200,
        request=_NS(method="GET", resource_type="xhr"),
    )
    img = _NS(
        url="https://example.com/logo.png", status=200,
        request=_NS(method="GET", resource_type="image"),
    )
    handlers["response"](xhr)
    handlers["response"](img)
    assert network_log == [
        {
            "url": "https://api.example.com/data",
            "method": "GET",
            "status": 200,
            "resource_type": "xhr",
        }
    ]


def test_browser_agent_capture_page_reports_truncation():
    class _FakeLocator:
        def inner_text(self, timeout=None):
            return "x" * (browser_agent._TEXT_TRUNCATE + 10)

    class _FakePage:
        url = "https://example.com/"

        def title(self):
            return "t"

        def content(self):
            return "<html>" + "y" * browser_agent._HTML_TRUNCATE + "</html>"

        def locator(self, sel):
            return _FakeLocator()

        def eval_on_selector_all(self, sel, js):
            return []

        def screenshot(self, full_page=None, type=None):
            return b"\x89PNG\r\n\x1a\n" + b"0" * 20

    capture = browser_agent._capture_page(_FakePage(), "scrape")
    assert capture["html_truncated"] is True
    assert capture["visible_text_truncated"] is True


def test_lighthouse_resolve_chrome_path_prefers_env_override(monkeypatch, tmp_path):
    from agents import lighthouse_auditor as la

    fake_chrome = tmp_path / "my-chrome"
    fake_chrome.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CHROME_PATH", str(fake_chrome))
    assert la._resolve_chrome_path() == str(fake_chrome)


def test_lighthouse_resolve_chrome_path_falls_back_to_system_then_playwright(
    monkeypatch, tmp_path
):
    """The 2026-05-18 critical fix (every call died ChromePathNotSetError)
    shipped without a regression test — this pins the fallback chain:
    CHROME_PATH env -> system chrome -> Playwright cache glob -> None."""
    from agents import lighthouse_auditor as la

    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(la.shutil, "which", lambda name: None)

    cache = tmp_path / "ms-playwright"
    chrome = cache / "chromium-1200" / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    newer = cache / "chromium-1210" / "chrome-linux64" / "chrome"
    newer.parent.mkdir(parents=True)
    newer.write_text("#!/bin/sh\n")
    newer.chmod(0o755)
    monkeypatch.setattr(la, "_PLAYWRIGHT_CACHE_DIRS", (str(cache),))
    assert la._resolve_chrome_path() == str(newer), "newest chromium must win"

    monkeypatch.setattr(la, "_PLAYWRIGHT_CACHE_DIRS", (str(tmp_path / "missing"),))
    assert la._resolve_chrome_path() is None


def test_lighthouse_resolve_chrome_path_walk_prefers_full_chrome(monkeypatch, tmp_path):
    """When the glob patterns miss (cache layout changed), the os.walk
    fallback must still find a binary and prefer chrome over headless-shell."""
    from agents import lighthouse_auditor as la

    monkeypatch.delenv("CHROME_PATH", raising=False)
    monkeypatch.setattr(la.shutil, "which", lambda name: None)
    cache = tmp_path / "ms-playwright"
    shell = cache / "weird-layout" / "chrome-headless-shell"
    chrome = cache / "weird-layout" / "sub" / "chrome"
    for p in (shell, chrome):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    monkeypatch.setattr(la, "_PLAYWRIGHT_CACHE_DIRS", (str(cache),))
    assert la._resolve_chrome_path() == str(chrome)


def test_lighthouse_build_cmd_throttling():
    from agents import lighthouse_auditor as la

    default_mobile = la._build_cmd("https://x.com", ["performance"], "mobile", "/tmp/o.json", "")
    assert "--throttling-method=simulate" in default_mobile
    default_desktop = la._build_cmd("https://x.com", ["performance"], "desktop", "/tmp/o.json", "")
    assert "--throttling-method=provided" in default_desktop
    devtools = la._build_cmd("https://x.com", ["performance"], "mobile", "/tmp/o.json", "devtools")
    assert "--throttling-method=devtools" in devtools


def test_lighthouse_rejects_invalid_throttling():
    from agents import lighthouse_auditor as la

    result = la.run({"url": "https://example.com", "throttling": "4g"})
    assert result["error"]["code"] == "lighthouse_auditor.invalid_throttling"


def test_lighthouse_opportunity_extraction_handles_modern_shapes():
    from agents import lighthouse_auditor as la

    audits = {
        "classic": {
            "title": "Classic opportunity",
            "details": {"type": "opportunity", "overallSavingsMs": 1200},
        },
        "metric-savings-only": {
            "title": "Modern opportunity",
            "details": {"type": "opportunity"},
            "metricSavings": {"LCP": 800, "FCP": 300},
        },
        "bytes-only": {
            "title": "Image formats",
            "details": {"type": "opportunity", "overallSavingsBytes": 50_000},
        },
        "garbage-details": {"title": "ignore me", "details": "not-a-dict"},
        "zero-savings": {"title": "noop", "details": {"type": "opportunity"}},
    }
    opps = la._extract_top_opportunities(audits)
    ids = [o["id"] for o in opps]
    assert ids[0] == "classic"
    assert "metric-savings-only" in ids
    assert "bytes-only" in ids
    assert "garbage-details" not in ids and "zero-savings" not in ids
    modern = next(o for o in opps if o["id"] == "metric-savings-only")
    assert modern["savings_ms"] == 800
    bytes_only = next(o for o in opps if o["id"] == "bytes-only")
    assert bytes_only["savings_bytes"] == 50_000


# ---------------------------------------------------------------------------
# /review + /cso remediation: regression tests for the fixes
# ---------------------------------------------------------------------------


def test_manifest_pypi_multi_ge_bound_picks_numerically_lowest():
    """Regression: lexicographic sort put '10.5' before '2.0', feeding OSV
    the wrong installed version."""
    from agents._manifest_parsing import parse_pypi_manifest

    for manifest in ("pkg>=2.0,>=10.5\n", "pkg>=10.5,>=2.0\n"):
        pkgs, _ = parse_pypi_manifest(manifest)
        assert pkgs == [("pkg", "2.0")], manifest


def test_manifest_npm_upper_bound_only_is_unpinned():
    """Regression: '<2.0.0' returned the ceiling as if it were installed."""
    from agents._manifest_parsing import _npm_version_from_spec

    assert _npm_version_from_spec("<2.0.0") == ("", None)
    assert _npm_version_from_spec("<=3.1") == ("", None)
    # A real range still yields its lower bound.
    assert _npm_version_from_spec(">=1.2.0 <2.0.0") == ("1.2.0", None)


def test_dns_inspector_redirect_to_internal_host_blocked(monkeypatch):
    """SSRF: a redirect to a private/metadata host must be refused per-hop,
    not followed (and its URL/headers echoed back)."""
    from agents import dns_inspector

    handler = dns_inspector._RecordingRedirectHandler()

    class _Req:
        full_url = "http://example.com"

    # validate_outbound_url raises for the internal target.
    monkeypatch.setattr(
        dns_inspector,
        "validate_outbound_url",
        lambda url, field: (_ for _ in ()).throw(ValueError("blocked private IP")),
    )
    import urllib.error

    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            _Req(), None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
        )


def test_dns_inspector_transport_failure_not_reported_as_absence(monkeypatch):
    """A resolver timeout must NOT be reported as 'No MX records found' —
    that conflates outage with definitive absence."""
    from agents import dns_inspector

    class _TimeoutResolver:
        def resolve(self, name, rtype):
            raise RuntimeError("LifetimeTimeout")  # not NXDOMAIN/NoAnswer

    monkeypatch.setattr(
        dns_inspector, "_dnspython_resolver", lambda: _TimeoutResolver()
    )
    result = dns_inspector.run(
        {"domains": ["example.com"], "checks": ["mx", "txt", "dmarc"]}
    )
    entry = result["results"][0]
    assert "No MX records found" not in entry["issues"]
    assert entry["txt"] is None  # transport failure -> could not run
    assert entry["dmarc"] is None


def test_dns_inspector_txt_dmarc_null_without_dnspython(monkeypatch):
    """Documented contract: dnspython absent -> txt/dmarc are None (not [])."""
    from agents import dns_inspector

    monkeypatch.setattr(dns_inspector, "_dnspython_resolver", lambda: None)
    result = dns_inspector.run(
        {"domains": ["example.com"], "checks": ["txt", "dmarc"]}
    )
    entry = result["results"][0]
    assert entry["txt"] is None
    assert entry["spf"] is None
    assert entry["dmarc"] is None
    assert "No SPF record found" not in entry["issues"]


def test_cve_lookup_kev_enrichment_in_cve_id_mode(monkeypatch):
    """KEV stamping runs in cve_ids mode (id_key='cve_id'), not just package
    mode — a regression reordering normalization would silently no-op it."""
    from agents import _kev_feed

    _patch_cve_network(monkeypatch)
    monkeypatch.setattr(
        _kev_feed,
        "_fetch_catalog",
        lambda: {"CVE-2024-99999": {"date_added": "2024-03-01", "ransomware": True}},
    )
    _kev_feed.reset_cache()
    result = cve_lookup.run({"cve_id": "CVE-2024-99999"})
    row = result["results"][0]
    assert row["exploit_source"] == "cisa_kev"
    assert row["kev"] == {"listed": True, "date_added": "2024-03-01"}


def test_kev_feed_failure_cooldown(monkeypatch):
    """A failed fetch is cached for the cooldown window, then retried —
    a dead feed must not add a 10s timeout to every call."""
    from agents import _kev_feed

    calls = {"n": 0}

    def fail():
        calls["n"] += 1
        return None

    monkeypatch.setattr(_kev_feed, "_fetch_catalog", fail)
    monkeypatch.setattr(_kev_feed.time, "time", lambda: 1000.0)
    _kev_feed.reset_cache()
    assert _kev_feed.kev_entries(["CVE-2024-1"]) is None
    assert _kev_feed.kev_entries(["CVE-2024-1"]) is None
    assert calls["n"] == 1, "within cooldown, no re-fetch"

    # Past the cooldown -> one more fetch.
    monkeypatch.setattr(
        _kev_feed.time, "time", lambda: 1000.0 + _kev_feed._FETCH_FAILURE_COOLDOWN_S + 1
    )
    _kev_feed.kev_entries(["CVE-2024-1"])
    assert calls["n"] == 2


def test_db_sandbox_no_index_hint_for_subquery_scan():
    """SCAN SUBQUERY / SCAN CONSTANT name derived ops, not real tables —
    no addable index, so no suggestion."""
    from agents import db_sandbox

    suggestions = db_sandbox._index_suggestions([
        {"query_plan": [
            {"detail": "SCAN SUBQUERY 1"},
            {"detail": "SCAN CONSTANT ROW"},
        ]}
    ])
    assert suggestions == []


def test_live_sandbox_payload_cap_measured_in_bytes():
    """The cap is bytes: a multi-byte-char payload just under the char count
    but over the byte cap must still be rejected."""
    from agents import live_sandbox

    # Each '€' is 3 UTF-8 bytes; 100k of them = 300KB > 256KB cap but only
    # 100k chars.
    payload = {"action": "sandbox_exec", "input": {"cmd": "€" * 100_000}}
    result = live_sandbox.run(payload)
    assert result["error"]["code"] == "live_sandbox.payload_too_large"


def test_browser_agent_network_types_rejects_non_list():
    from agents import browser_agent

    err = browser_agent._normalize_network_types("xhr")
    assert err["error"]["code"] == "browser_agent.invalid_network_types"


def test_accessibility_empty_vendored_axe_not_cached(monkeypatch, tmp_path):
    """A truncated/empty vendored read must not poison the cache for the
    worker's lifetime."""
    from agents import accessibility_auditor as aa

    empty = tmp_path / "axe.min.js"
    empty.write_text("")
    monkeypatch.setattr(aa, "_VENDORED_AXE_PATH", empty)
    monkeypatch.setattr(aa, "_vendored_axe_cache", None)
    assert aa._vendored_axe_source() is None
    # Now a good file appears — must be picked up (not stuck on cached "").
    empty.write_text("/*! axe v4.8.4 */ window.axe={};")
    assert aa._vendored_axe_source() is not None
