from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents import browser_agent
from agents import cve_lookup
from agents import db_sandbox
from agents import linter_agent
from agents import live_endpoint_tester
from agents import semantic_codebase_search
from agents import type_checker
from agents import visual_regression


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return dict(self._payload)


def test_cve_lookup_prefers_nvd_for_package_search(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None):
        del timeout, headers
        assert url == cve_lookup._NVD_API
        assert params == {"keywordSearch": "lodash", "resultsPerPage": 20}
        return _FakeResponse(
            200,
            {
                "vulnerabilities": [
                    {
                        "cve": {
                            "id": "CVE-2024-12345",
                            "published": "2024-01-02T00:00:00.000",
                            "lastModified": "2024-01-03T00:00:00.000",
                            "metrics": {
                                "cvssMetricV31": [
                                    {"cvssData": {"baseScore": 8.8}},
                                ]
                            },
                            "descriptions": [
                                {"lang": "en", "value": "Prototype pollution issue"},
                            ],
                        }
                    }
                ]
            },
        )

    def fail_post(*args, **kwargs):
        raise AssertionError("OSV fallback should not run when NVD succeeds")

    monkeypatch.setattr(cve_lookup.requests, "get", fake_get)
    monkeypatch.setattr(cve_lookup.requests, "post", fail_post)

    result = cve_lookup.run({"packages": ["lodash@4.17.20"]})
    assert result["source"] == "nvd"
    assert result["results"][0]["cve"] == "CVE-2024-12345"
    assert result["results"][0]["severity"] == "high"


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


def test_type_checker_parses_mypy_json(monkeypatch):
    def fake_run(cmd, capture_output=False, text=False, timeout=None, cwd=None, **kwargs):
        del capture_output, text, timeout, cwd, kwargs
        if cmd[:4] == ["python3", "-m", "mypy", "--version"]:
            return SimpleNamespace(returncode=0, stdout="mypy 1.11.2\n", stderr="")
        if cmd[:3] == ["python3", "-m", "mypy"]:
            payload = [
                {
                    "file": "main.py",
                    "line": 1,
                    "column": 5,
                    "code": "arg-type",
                    "message": 'Argument 1 to "f" has incompatible type "str"; expected "int"',
                    "severity": "error",
                }
            ]
            return SimpleNamespace(returncode=1, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"Unexpected subprocess command: {cmd!r}")

    monkeypatch.setattr(type_checker.subprocess, "run", fake_run)

    result = type_checker.run({"language": "python", "code": "def f(x: int) -> None:\n    pass\nf('x')"})
    assert result["passed"] is False
    assert result["ok"] is False
    assert result["tool_version"] == "mypy 1.11.2"
    assert result["error_count"] == 1
    assert result["diagnostics"][0]["code"] == "arg-type"


def test_linter_agent_returns_tool_unavailable_for_js_without_node(monkeypatch):
    monkeypatch.setattr(linter_agent.shutil, "which", lambda name: None)
    result = linter_agent.run({"language": "javascript", "code": "const x = y;"})
    assert result["error"]["code"] == "linter_agent.tool_unavailable"


def test_linter_agent_uses_ruff_for_python(monkeypatch):
    monkeypatch.setattr(linter_agent.shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        del capture_output, text, timeout, kwargs
        assert cmd[0] == "ruff"
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                [
                    {
                        "code": "F401",
                        "message": "`os` imported but unused",
                        "location": {"row": 1, "column": 8},
                        "fix": None,
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(linter_agent.subprocess, "run", fake_run)
    result = linter_agent.run({"language": "python", "code": "import os\n"})
    assert result["tool"] == "ruff"
    assert result["error_count"] == 1
    assert result["issues"][0]["rule"] == "F401"


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


def test_live_endpoint_tester_uses_mocked_upstream(monkeypatch):
    class _FakeResponse:
        ok = True
        status_code = 200
        content = b"ok"

    class _FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def request(self, method, url, headers=None, json=None, data=None, timeout=None, allow_redirects=None):
            del headers, json, data, timeout, allow_redirects
            self.calls += 1
            assert method == "GET"
            assert url == "https://example.com/health"
            return _FakeResponse()

        def close(self):
            return None

    monkeypatch.setattr(live_endpoint_tester.requests, "Session", _FakeSession)
    result = live_endpoint_tester.run({"url": "https://example.com/health", "requests": 5, "concurrency": 2})
    assert result["success_count"] == 5
    assert result["failure_count"] == 0
    assert result["status_counts"]["200"] == 5


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
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self) -> None:
            return None

    payloads = [left.getvalue(), right.getvalue()]

    def fake_get(url, timeout=None, headers=None):
        del timeout, headers
        assert url.startswith("https://example.com/")
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


def test_semantic_codebase_search_rejects_invalid_git_url_via_ssrf_guard():
    result = semantic_codebase_search.run({"query": "pdf extraction", "git_url": "ftp://example.com/repo"})
    assert result["error"]["code"] == "semantic_codebase_search.url_blocked"
    assert "absolute http(s) URL" in result["error"]["message"]


def test_semantic_codebase_search_rejects_ambiguous_source_inputs():
    result = semantic_codebase_search.run(
        {
            "query": "pdf extraction",
            "git_url": "https://github.com/example/repo",
            "artifact": {"url_or_base64": "Zm9v", "name": "repo.zip"},
        }
    )
    assert result["error"]["code"] == "semantic_codebase_search.ambiguous_source"
