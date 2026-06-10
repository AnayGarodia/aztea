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



