from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agents import browser_agent
from agents import codereview
from agents import cve_lookup
from agents import dependency_auditor
from agents import db_sandbox
from agents import hn_digest
from agents import linter_agent
from agents import live_endpoint_tester
from agents import multi_file_executor
from agents import python_executor
from agents import shell_executor
from agents import semantic_codebase_search
from agents import type_checker
from agents import video_storyboard
from agents import visual_regression
from agents import arxiv_research
from agents import web_researcher
from agents import wiki
from agents.financial import synthesizer as financial_synthesizer


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

    latest, license_ = dependency_auditor._fetch_npm_latest("lodash")
    assert latest == "4.17.21"
    assert license_ == "MIT"


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


def test_code_review_accepts_diff_and_normalizes_output(monkeypatch):
    monkeypatch.setattr(
        codereview,
        "run_with_fallback",
        lambda req: SimpleNamespace(
            text=json.dumps(
                {
                    "language_detected": "python",
                    "score": 7,
                    "security_critical": False,
                    "complexity_score": 3,
                    "issues": [
                        {
                            "line_hint": "@@ ... return a / b",
                            "severity": "medium",
                            "category": "correctness",
                            "description": "Missing zero guard",
                            "fix": "Handle b == 0 before division.",
                        }
                    ],
                    "positive_aspects": ["Small focused change."],
                    "test_recommendations": ["Cover b == 0."],
                    "summary": "One correctness issue found.",
                }
            )
        ),
    )
    result = codereview.run(diff="@@ -1 +1 @@\n-return a / b\n+return a / b\n", language="python", filename="math_utils.py")
    assert result["review_target"] == "diff"
    assert result["filename"] == "math_utils.py"
    assert result["issue_count"] == 1
    assert result["severity_counts"]["medium"] == 1
    assert result["issues"][0]["category"] == "correctness"


def test_code_review_requires_code_or_diff():
    result = codereview.run()
    assert result["error"]["code"] == "code_review_agent.missing_input"


def test_code_review_downgrades_plain_divide_by_zero_from_security(monkeypatch):
    monkeypatch.setattr(
        codereview,
        "run_with_fallback",
        lambda req: SimpleNamespace(
            text=json.dumps(
                {
                    "language_detected": "python",
                    "score": 3,
                    "complexity_score": 2,
                    "issues": [
                        {
                            "line_hint": "return a / b",
                            "severity": "critical",
                            "category": "security",
                            "cwe_id": "CWE-369",
                            "owasp_category": "A03 Injection",
                            "description": "Potential divide-by-zero if b is 0.",
                            "fix": "Validate b before division.",
                        }
                    ],
                    "summary": "One critical security issue found.",
                }
            )
        ),
    )
    result = codereview.run(code="def divide(a, b):\n    return a / b\n", language="python")
    issue = result["issues"][0]
    assert issue["severity"] == "medium"
    assert issue["category"] == "correctness"
    assert issue["cwe_id"] is None
    assert issue["owasp_category"] is None
    assert result["security_critical"] is False


def test_code_review_falls_back_to_rule_based_review_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr(codereview, "run_with_fallback", lambda req: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = codereview.run(
        diff="@@ -1 +1 @@\n- console.log('[redacted]')\n+ console.log(token)\n",
        language="javascript",
        filename="auth.js",
        focus="security",
    )
    assert result["llm_used"] is False
    assert result["degraded_mode"] is True
    assert result["issue_count"] >= 1
    assert result["issues"][0]["cwe_id"] == "CWE-532"


def test_type_checker_returns_structured_error_for_missing_code():
    result = type_checker.run({"language": "python"})
    assert result["error"]["code"] == "type_checker.missing_code"


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


def test_linter_agent_returns_structured_error_for_missing_code():
    result = linter_agent.run({"language": "python"})
    assert result["error"]["code"] == "linter_agent.missing_code"


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
            del headers, json, data, timeout
            self.calls += 1
            assert method == "GET"
            assert url == "https://example.com/health"
            assert allow_redirects is False
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
        # status_code is required — the agent's redirect/error guard reads it
        # before processing the body. Without it the agent short-circuits with
        # ``visual_regression.decode_failed`` and never reaches the diff code.
        def __init__(self, content: bytes, status_code: int = 200):
            self.content = content
            self.status_code = status_code

        def raise_for_status(self) -> None:
            return None

    payloads = [left.getvalue(), right.getvalue()]

    def fake_get(url, timeout=None, headers=None, allow_redirects=None):
        del timeout, headers
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


def test_hn_digest_returns_structured_error_on_timeout(monkeypatch):
    def fake_get(*args, **kwargs):
        del args, kwargs
        raise hn_digest.httpx.TimeoutException("boom")

    monkeypatch.setattr(hn_digest.httpx, "get", fake_get)
    result = hn_digest.run({})
    assert result["error"]["code"] == "hn_digest.timeout"


def test_hn_digest_degrades_gracefully_without_llm(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "hits": [
                    {
                        "title": "New compiler release",
                        "url": "https://example.com/compiler",
                        "points": 123,
                        "num_comments": 45,
                        "author": "alice",
                        "created_at": "2026-04-28T00:00:00Z",
                    }
                ]
            }

    monkeypatch.setattr(hn_digest.httpx, "get", lambda *args, **kwargs: _FakeResponse())
    monkeypatch.setattr(hn_digest, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = hn_digest.run({"count": 1})
    assert result["stories"][0]["title"] == "New compiler release"
    assert "no LLM provider" in result["synthesis"]


def test_python_executor_returns_structured_error_for_missing_code():
    result = python_executor.run({})
    assert result["error"]["code"] == "python_executor.missing_code"


def test_arxiv_research_returns_structured_error_for_missing_query():
    result = arxiv_research.run({})
    assert result["error"]["code"] == "arxiv_research.missing_query"


def test_arxiv_research_degrades_gracefully_without_llm(monkeypatch):
    monkeypatch.setattr(
        arxiv_research,
        "_fetch_arxiv",
        lambda *args, **kwargs: [
            {
                "arxiv_id": "2404.12345",
                "title": "Transformer Systems",
                "authors": ["Alice", "Bob"],
                "abstract": "A paper about transformers.",
                "categories": ["cs.AI"],
                "published": "2024-04-01",
                "updated": "2024-04-02",
                "pdf_url": "https://arxiv.org/pdf/2404.12345",
                "abstract_url": "https://arxiv.org/abs/2404.12345",
            }
        ],
    )
    monkeypatch.setattr(arxiv_research, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = arxiv_research.run({"query": "transformers", "max_results": 1})
    assert result["total_found"] == 1
    assert "no LLM provider" in result["synthesis"]


def test_web_researcher_degrades_gracefully_without_llm(monkeypatch):
    monkeypatch.setattr(
        web_researcher,
        "_fetch_one",
        lambda url: {
            "url": url,
            "content": "Example article body with useful details.",
            "status": "ok",
            "links": [],
            "html_title": "Example",
            "word_count": 6,
        },
    )
    monkeypatch.setattr(web_researcher, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = web_researcher.run({"url": "https://example.com/article"})
    assert result["title"] == "Example"
    assert result["summary"]
    assert result["billing_units_actual"] == 1


def test_web_researcher_returns_structured_error_when_all_fetches_fail(monkeypatch):
    monkeypatch.setattr(
        web_researcher,
        "_fetch_one",
        lambda url: {
            "url": url,
            "content": None,
            "status": "error",
            "error": "fetch failed",
        },
    )
    result = web_researcher.run({"urls": ["https://example.com/one", "https://example.com/two"]})
    assert result["error"]["code"] == "web_researcher.all_fetches_failed"


def test_wiki_degrades_gracefully_without_llm(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "title": "Example Topic",
                "extract": "Example Topic is a notable thing with a history worth reading.",
                "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Example_Topic"}},
            }

    monkeypatch.setattr(wiki.requests, "get", lambda *args, **kwargs: _FakeResponse())
    monkeypatch.setattr(wiki, "run_with_fallback", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no llm")))
    result = wiki.run("Example Topic")
    assert result["title"] == "Example Topic"
    assert "Example Topic is a notable thing" in result["summary"]
    assert result["key_facts"]
    assert result["llm_used"] is False


def test_financial_synthesizer_returns_grounded_fallback_without_llm(monkeypatch):
    monkeypatch.setattr(
        financial_synthesizer,
        "run_with_fallback",
        lambda req: (_ for _ in ()).throw(RuntimeError("no llm")),
    )
    result = financial_synthesizer.synthesize_brief(
        {
            "ticker": "TEST",
            "company_name": "Test Corp",
            "filing_type": "10-Q",
            "filing_date": "2026-01-31",
            "document_url": "https://sec.example/test",
            "text": (
                "Test Corp develops enterprise software and support services. "
                "Revenue increased to $10.2 billion during the quarter. "
                "Operating cash flow was $2.1 billion. "
                "The company warned that competition and supply chain risk could pressure margins."
            ),
        }
    )
    assert result["llm_used"] is False
    assert result["degraded_mode"] is True
    assert result["recent_financial_highlights"]
    assert result["key_risks"]
    assert result["signal"] in {"positive", "neutral", "negative"}
    assert result["source_evidence"]["financial_highlights"]


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


def test_semantic_codebase_search_rejects_zip_traversal_artifact():
    # Build a valid ZIP with a path-traversal entry (``../evil.txt``) at runtime
    # rather than hardcoding base64 — the previously inlined string had broken
    # padding and decoded as garbage, causing the agent to fail at the decode
    # step before the traversal guard could fire.
    import base64
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("../evil.txt", "hello")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")

    payload = {
        "query": "pdf extraction",
        "artifact": {
            "name": "repo.zip",
            "url_or_base64": f"data:application/zip;base64,{encoded}",
        },
    }
    result = semantic_codebase_search.run(payload)
    assert result["error"]["code"] == "semantic_codebase_search.unsafe_artifact"
    assert "unsafe traversal" in result["error"]["message"]


def test_semantic_codebase_search_lexical_fallback_returns_relevant_file(monkeypatch):
    monkeypatch.setattr(
        semantic_codebase_search,
        "_extract_artifact",
        lambda artifact, extensions, max_file_bytes: {
            "src/pillow_fix.py": "def fix_cve_2023_50447():\n    return 'patched'\n",
            "README.md": "general project overview",
        },
    )
    monkeypatch.setattr(semantic_codebase_search, "DISABLE_EMBEDDINGS", True)
    result = semantic_codebase_search.run(
        {
            "query": "CVE-2023-50447 pillow fix",
            "artifact": {"name": "repo.zip", "url_or_base64": "Zm9v"},
        }
    )
    assert result["results"]
    assert result["results"][0]["path"] == "src/pillow_fix.py"
    assert "cve_2023_50447" in result["results"][0]["snippet"].lower()
    assert result["results"][0]["line_start"] >= 1
    assert result["results"][0]["line_end"] >= result["results"][0]["line_start"]


def test_browser_agent_rejects_invalid_action():
    result = browser_agent.run({"url": "https://example.com", "action": "interact"})
    assert result["error"]["code"] == "browser_agent.invalid_action"


def test_multi_file_executor_returns_structured_error_for_invalid_files():
    result = multi_file_executor.run({"files": {"main.py": "print('hi')"}})
    assert result["error"]["code"] == "multi_file_executor.invalid_input"


def test_web_researcher_blocks_redirects_to_private_targets(monkeypatch):
    """Redirects to public URLs are followed; redirects to private/internal
    targets are blocked at the SSRF gate. Pre-2026-05-03 we blocked all
    redirects, which broke fetches against major sites that 301 to www.
    Now we follow safe hops and only refuse when the redirect target is
    itself unsafe (localhost, RFC1918, link-local, etc.)."""

    class _FakeRedirect:
        status_code = 302
        headers = {"Location": "http://127.0.0.1/internal"}

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, timeout=None, headers=None, allow_redirects=None, stream=None):
        del timeout, headers, stream
        assert allow_redirects is False
        return _FakeRedirect()

    monkeypatch.setattr(web_researcher.requests, "get", fake_get)
    result = web_researcher.run({"url": "https://example.com/article"})
    assert result["error"]["code"] == "web_researcher.fetch_failed"
    assert "redirect_blocked" in result["error"]["message"]


def test_browser_agent_request_guard_aborts_private_subrequests():
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


def test_video_storyboard_returns_structured_error_when_backend_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        video_storyboard,
        "_generate_video_artifact",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("REPLICATE_VIDEO_MODEL is required for video generation.")),
    )
    result = video_storyboard.run({"brief": "A launch teaser for an AI product."})
    assert result["error"]["code"] == "video_storyboard.not_configured"


def test_shell_executor_does_not_forward_host_secrets(monkeypatch):
    monkeypatch.setenv("AZTEA_API_KEY", "super-secret")
    captured: dict[str, str] = {}

    def fake_run(cmd, capture_output=None, text=None, timeout=None, cwd=None, env=None):
        del cmd, capture_output, text, timeout, cwd
        captured.update(env or {})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(shell_executor.subprocess, "run", fake_run)
    result = shell_executor.run({"command": "python -V"})
    assert result["exit_code"] == 0
    assert "AZTEA_API_KEY" not in captured


def test_type_checker_falls_back_to_npx_when_tsc_missing(monkeypatch):
    """If global tsc is absent, _run_tsc should use npx --package typescript tsc."""
    import importlib
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None if name == "tsc" else f"/usr/bin/{name}")

    import subprocess as _subprocess
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(_subprocess, "run", fake_run)

    import agents.type_checker as tc
    importlib.reload(tc)
    tc._run_tsc("const x: number = 1;", {}, False)
    assert "cmd" in captured, "subprocess.run was never called — patch may be broken"
    assert captured["cmd"][0] == "npx", "should fall back to npx"
    assert "--package" in captured["cmd"], "must specify --package typescript"
    assert "tsc" in captured["cmd"]
