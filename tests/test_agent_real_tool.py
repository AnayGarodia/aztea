from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents import browser_agent
from agents import cve_lookup
from agents import dependency_auditor
from agents import db_sandbox
from agents import python_executor
from agents import secret_scanner
from agents import visual_regression


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return dict(self._payload)


def test_cve_lookup_prefers_osv_for_package_search(monkeypatch):
    """OSV.dev is the canonical source for package@version lookups.

    The NVD ``keywordSearch`` endpoint matches the package string against every
    CVE description and returns false positives (e.g. "express" → Outlook
    Express, Intel Express switches). The 2026-04-28 audit caught exactly that
    leak in production, so the agent now queries OSV first and never falls
    back to NVD keyword search for package mode.
    """
    osv_calls: list[dict] = []

    def fake_post(url, json=None, timeout=None, headers=None):
        del timeout, headers
        assert url == cve_lookup._OSV_API
        osv_calls.append(json)
        return _FakeResponse(
            200,
            {
                "vulns": [
                    {
                        "id": "GHSA-xxxx",
                        "aliases": ["CVE-2024-12345"],
                        "summary": "Prototype pollution issue",
                        "published": "2024-01-02T00:00:00.000",
                        "modified": "2024-01-03T00:00:00.000",
                        "severity": [{"score": "8.8"}],
                        "affected": [{"ranges": [{"events": [{"fixed": "4.17.21"}]}]}],
                    }
                ]
            },
        )

    def fake_get(url, params=None, timeout=None, headers=None):
        del timeout, headers
        assert url == cve_lookup._NVD_API
        assert params == {"cveId": "CVE-2024-12345"}
        return _FakeResponse(
            200,
            {
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2024-12345",
                            "published": "2024-01-02T00:00:00.000",
                            "lastModified": "2024-01-03T00:00:00.000",
                            "descriptions": [{"lang": "en", "value": "Prototype pollution issue"}],
                            "metrics": {"cvssMetricV31": [{"cvssData": {"baseScore": 9.1}}]},
                            "references": [],
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(cve_lookup.requests, "post", fake_post)
    monkeypatch.setattr(cve_lookup.requests, "get", fake_get)

    result = cve_lookup.run({"packages": ["lodash@4.17.20"]})
    assert result["source"] == "osv+nvd"
    assert result["results"][0]["cve"] == "CVE-2024-12345"
    assert result["results"][0]["severity"] == "critical"
    assert result["results"][0]["cvss"] == 9.1
    # And OSV was queried for the right package + version + ecosystem.
    assert osv_calls and osv_calls[0]["package"]["name"] == "lodash"
    assert osv_calls[0]["version"] == "4.17.20"


def test_cve_lookup_falls_back_to_osv_for_direct_cve_id(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        del timeout, headers
        if url == cve_lookup._NVD_API:
            assert params == {"cveId": "CVE-2024-55555"}
            return _FakeResponse(503, {})
        assert url.endswith("/CVE-2024-55555")
        return _FakeResponse(
            200,
            {
                "id": "GHSA-xxxx",
                "aliases": ["CVE-2024-55555"],
                "summary": "Fallback advisory",
                "published": "2024-02-01T00:00:00.000",
                "modified": "2024-02-02T00:00:00.000",
                "severity": [{"score": "CVSS_V3:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/9.8"}],
                "references": [{"url": "https://osv.dev/example"}],
            },
        )

    monkeypatch.setattr(cve_lookup.requests, "get", fake_get)

    result = cve_lookup.run({"cve_id": "CVE-2024-55555"})
    assert result["source"] == "osv"
    assert result["cve_id"] == "CVE-2024-55555"
    assert result["severity"] == "critical"


def test_dependency_auditor_ignores_invalid_npm_latest_dist_tag(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        del timeout, headers
        assert url.endswith("/lodash")
        return _FakeResponse(
            200,
            {
                "dist-tags": {"latest": "4.18.1"},
                "versions": {
                    "4.17.20": {"license": "MIT"},
                    "4.17.21": {"license": "MIT"},
                },
            },
        )

    monkeypatch.setattr(dependency_auditor.requests, "get", fake_get)

    latest, license_, not_found = dependency_auditor._fetch_npm_latest("lodash")
    assert latest == "4.17.21"
    assert license_ == "MIT"
    assert not_found is False


def test_dependency_auditor_rejects_freeform_garbage_manifest():
    result = dependency_auditor.run({"manifest": "this is not a manifest at all"})
    assert result["error"]["code"] == "dependency_auditor.invalid_manifest"


def test_cve_lookup_rejects_mutually_exclusive_id_fields():
    result = cve_lookup.run({"cve_id": "CVE-2024-1234", "cve_ids": ["CVE-2024-5678"]})
    assert result["error"]["code"] == "cve_lookup.mutually_exclusive_ids"


def test_db_sandbox_executes_sql_and_returns_plan():
    result = db_sandbox.run(
        {
            "schema_sql": "CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO items(name) VALUES ('a'), ('b');",
            "sql": "SELECT name FROM items ORDER BY id",
        }
    )
    assert result["engine"] == "sqlite"
    assert result["results"][0]["rows"] == [{"name": "a"}, {"name": "b"}]
    assert result["results"][0]["query_plan"]


def test_db_sandbox_accepts_string_queries_and_keeps_partial_errors():
    result = db_sandbox.run(
        {
            "schema_sql": "CREATE TABLE t(id INTEGER); INSERT INTO t VALUES (1);",
            "queries": ["SELECT count(*) AS n FROM t", "DROP TABLE t", "SELECT * FROM t"],
        }
    )
    assert result["statements_executed"] == 3
    assert result["results"][0]["rows"][0]["n"] == 1
    assert "error" not in result["results"][1]
    assert result["results"][2]["error"]["code"] == "db_sandbox.sql_error"
    assert result["statement_error_count"] == 1


def test_secret_scanner_negative_entropy_disables_entropy_check():
    result = secret_scanner.run({"content": "plain text only", "min_entropy": -1})
    assert result["total_findings"] == 0


def test_visual_regression_returns_annotated_artifact(monkeypatch):
    pytest.importorskip("PIL")
    from PIL import Image
    import io

    left = io.BytesIO()
    right = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 255, 255, 255)).save(left, format="PNG")
    image = Image.new("RGBA", (8, 8), (255, 255, 255, 255))
    image.putpixel((4, 4), (255, 0, 0, 255))
    image.save(right, format="PNG")

    class _FakeResponse:
        # status_code is required — the agent's redirect/error guard reads it
        # before processing the body. Without it the agent short-circuits with
        # ``visual_regression.decode_failed`` and never reaches the diff code.
        def __init__(self, content: bytes, status_code: int = 200):
            self.content = content
            self.status_code = status_code
            self.headers = {}

        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size=None):
            yield self.content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    payloads = [left.getvalue(), right.getvalue()]

    def fake_get(url, timeout=None, headers=None, allow_redirects=None, stream=False):
        del timeout, headers, stream
        assert url.startswith("https://example.com/")
        assert allow_redirects is False
        return _FakeResponse(payloads.pop(0))

    monkeypatch.setattr(visual_regression.requests, "get", fake_get)
    result = visual_regression.run(
        {
            "left_url": "https://example.com/baseline.png",
            "right_url": "https://example.com/candidate.png",
        }
    )
    assert result["changed_pixels"] > 0
    assert result["artifacts"][0]["mime"] == "image/png"
    assert str(result["artifacts"][0]["url_or_base64"]).startswith("data:image/png;base64,")


def test_browser_agent_rejects_invalid_url_via_ssrf_guard():
    result = browser_agent.run({"url": "ftp://example.com"})
    assert result["error"]["code"] == "browser_agent.url_blocked"
    assert "absolute http(s) URL" in result["error"]["message"]


def test_python_executor_returns_structured_error_for_missing_code():
    result = python_executor.run({})
    assert result["error"]["code"] == "python_executor.missing_code"


def test_python_executor_traceback_line_numbers_match_user_code():
    result = python_executor.run({"code": "raise ValueError('oops')"})
    assert result["exit_code"] != 0
    stderr = result.get("stderr", "")
    import re
    # All "line N" in the traceback for single-line user code should be ~1
    line_nums = [int(m) for m in re.findall(r"\bline (\d+)\b", stderr)]
    high_lines = [n for n in line_nums if n > 5]
    assert not high_lines, (
        f"Traceback reports line(s) {high_lines} — expected ~line 1 for single-line code"
    )


def test_browser_agent_rejects_invalid_action():
    result = browser_agent.run({"url": "https://example.com", "action": "interact"})
    assert result["error"]["code"] == "browser_agent.invalid_action"


def test_browser_agent_request_guard_aborts_private_subrequests(monkeypatch):
    monkeypatch.delenv("ALLOW_PRIVATE_OUTBOUND_URLS", raising=False)  # force SSRF enforcement (dev .env sets it on)
    events: list[str] = []

    class _FakeRoute:
        def __init__(self, url: str):
            self.request = SimpleNamespace(url=url)

        def abort(self) -> None:
            events.append("abort")

        def continue_(self) -> None:
            events.append("continue")

    class _FakeContext:
        def __init__(self) -> None:
            self.handler = None

        def route(self, pattern: str, handler) -> None:
            assert pattern == "**/*"
            self.handler = handler

    context = _FakeContext()
    browser_agent._install_request_guard(context)
    context.handler(_FakeRoute("http://127.0.0.1/private"))
    context.handler(_FakeRoute("https://example.com/public"))
    assert events == ["abort", "continue"]


def test_db_sandbox_blocks_attach_database():
    result = db_sandbox.run({"sql": "ATTACH DATABASE '/etc/passwd' AS leak"})
    assert "error" in result
    code = result["error"]["code"]
    assert "blocked" in code or "attach" in code.lower()


def test_db_sandbox_blocks_attach_in_schema_sql():
    result = db_sandbox.run({
        "schema_sql": "ATTACH DATABASE '/tmp/other.db' AS other",
        "sql": "SELECT 1"
    })
    assert "error" in result


def test_db_sandbox_blocks_detach():
    result = db_sandbox.run({"sql": "DETACH DATABASE leak"})
    assert "error" in result





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


class _FakeResolver:
    """Minimal dns.resolver.Resolver stand-in keyed on (name, rtype)."""

    def __init__(self, table: dict):
        self._table = table

    def resolve(self, name, rtype):
        key = (name, rtype)
        if key not in self._table:
            raise RuntimeError(f"NXDOMAIN {key}")
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
