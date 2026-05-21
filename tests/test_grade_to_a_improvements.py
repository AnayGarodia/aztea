"""Regression tests for the 2026-05-20 grade-everything-to-A audit fixes.

# OWNS: locking in the behavior changes introduced in PR #84 so the next
#        catalog-wide refactor can't silently regress any of them.
# NOT OWNS: live integration of the agents against real services.
# DECISIONS:
#   - Every test is a pure unit test of the agent's `run(...)` (or a
#     small helper). No HTTP harness, no DB, no LLM. The point is to
#     pin a specific code path, not to re-test the whole dispatcher.
#   - Each test docs which audit grade-bump it locks in (so a future
#     reader sees the intent without spelunking the PR description).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# F-tier: quant_patch_validator runtime preflight
# ---------------------------------------------------------------------------


def test_quant_patch_validator_preflight_returns_structured_envelope_when_hypothesis_missing():
    """If hypothesis disappears from the worker, the agent returns a
    structured runtime_missing envelope — never a generic 500. Locks in
    the defensive preflight added 2026-05-20."""
    from agents.quant_patch_validator import _runtime_dep_preflight

    with patch("builtins.__import__", side_effect=ImportError("hypothesis missing")):
        result = _runtime_dep_preflight()
    assert result is not None
    assert result["error"]["code"] == "quant_patch_validator.runtime_missing"
    assert "hypothesis" in result["error"]["message"]


def test_quant_patch_validator_preflight_passes_when_hypothesis_installed():
    """When hypothesis IS importable, preflight returns None and the
    agent's normal flow continues."""
    from agents.quant_patch_validator import _runtime_dep_preflight

    # Hypothesis is in requirements.txt now; the preflight should be a no-op.
    assert _runtime_dep_preflight() is None


# ---------------------------------------------------------------------------
# openapi_validator: documented previous_spec field name
# ---------------------------------------------------------------------------


def test_openapi_validator_accepts_previous_spec_field():
    """The spec declares `previous_spec`; pre-fix the code looked for
    `base_spec` and silently returned breaking_changes: []. Locks in
    the dual-key acceptance."""
    from agents.openapi_validator import run

    v1 = (
        "openapi: 3.0.0\n"
        "info: { title: T, version: '1' }\n"
        "paths:\n"
        "  /admin: { get: { summary: a, responses: { '200': { description: ok } } } }\n"
        "  /users: { get: { summary: u, responses: { '200': { description: ok } } } }\n"
    )
    v2 = (
        "openapi: 3.0.0\n"
        "info: { title: T, version: '2' }\n"
        "paths:\n"
        "  /users: { get: { summary: u, responses: { '200': { description: ok } } } }\n"
    )

    # Pass the field name documented in the spec.
    result = run({"spec": v2, "previous_spec": v1, "format": "yaml"})
    assert "breaking_changes" in result
    assert len(result["breaking_changes"]) >= 1, result

    # Old-style base_spec field still works for backward compat.
    result2 = run({"spec": v2, "base_spec": v1, "format": "yaml"})
    assert len(result2["breaking_changes"]) >= 1, result2


# ---------------------------------------------------------------------------
# ci_failure_reproducer: LLM short-circuit on self-explanatory failures
# ---------------------------------------------------------------------------


def test_ci_failure_reproducer_skips_llm_on_self_explanatory_assertion():
    """When the failure is an AssertionError already shown in stderr,
    we don't burn an LLM call for hedging language. Locks in the
    self-explanatory short-circuit."""
    from agents.ci_failure_reproducer import _is_self_explanatory, _FT_CODE

    stderr = (
        "============================= test session starts ===========\n"
        "tests/test_x.py F                                       [100%]\n"
        "E   AssertionError: assert 4 == 5\n"
    )
    fragment = _is_self_explanatory(_FT_CODE, "", stderr)
    assert fragment is not None
    assert "Assertion" in fragment or "4 == 5" in fragment


def test_ci_failure_reproducer_does_not_short_circuit_for_dep_failures():
    """A `dependency_error` failure type still goes through the LLM
    because the diagnosis depends on context the raw output doesn't have."""
    from agents.ci_failure_reproducer import _is_self_explanatory, _FT_DEP

    stderr = "ModuleNotFoundError: No module named 'requests'\n"
    # _FT_DEP is NOT in the self-explanatory whitelist.
    assert _is_self_explanatory(_FT_DEP, "", stderr) is None


# ---------------------------------------------------------------------------
# coverage_runner: filename-gate relaxed to soft no_tests_discovered
# ---------------------------------------------------------------------------


def test_coverage_runner_no_longer_hard_rejects_non_test_named_files():
    """Pre-fix this returned coverage_runner.no_test_files even before
    pytest ran. Post-fix it lets pytest decide; on zero-discovery it
    surfaces no_tests_discovered with hints."""
    from agents.coverage_runner import run

    result = run({
        "files": [
            {"name": "myproject.py", "content": "def add(a, b):\n    return a + b\n"},
        ],
    })
    # Either the agent ran (and returned a coverage / runtime envelope) OR
    # it returned the new no_tests_discovered envelope with a hint. Pre-fix
    # it returned no_test_files which is no longer in the code path.
    code = result.get("error", {}).get("code", "")
    assert code != "coverage_runner.no_test_files", result
    # If pytest ran and discovered zero tests we should see the new code.
    if "error" in result:
        assert code in (
            "coverage_runner.no_tests_discovered",
            "coverage_runner.timeout",
            "coverage_runner.missing_files",
        ), result


# ---------------------------------------------------------------------------
# db_sandbox: invalid_schema_sql envelope on parse errors
# ---------------------------------------------------------------------------


def test_db_sandbox_returns_structured_envelope_on_malformed_schema():
    """A common shape-mistake (JSON-escaped backslash mangled by the
    transport) used to surface as `OperationalError: unrecognized token`
    with no triage hint. Now it returns invalid_schema_sql with a
    snippet quoted back."""
    from agents.db_sandbox import run

    result = run({
        # Intentionally malformed: stray backslash.
        "schema_sql": 'CREATE TABLE t (name TEXT\\);',
        "queries": [{"sql": "SELECT 1"}],
    })
    assert "error" in result
    assert result["error"]["code"] == "db_sandbox.invalid_schema_sql"
    assert "snippet" in result["error"].get("details", {}), result


def test_db_sandbox_happy_path_unaffected():
    """The new error handler doesn't swallow normal queries."""
    from agents.db_sandbox import run

    result = run({
        "schema_sql": "CREATE TABLE t (n INTEGER); INSERT INTO t VALUES (1);",
        "queries": [{"sql": "SELECT n FROM t"}],
    })
    assert "error" not in result, result
    assert result.get("results") or result.get("rows") or result, result


# ---------------------------------------------------------------------------
# broken_link_crawler: robots.txt disallow → robots_disallowed bucket
# ---------------------------------------------------------------------------


def test_broken_link_crawler_url_is_disallowed_helper():
    """The robots.txt gate moves disallowed URLs into their own bucket
    instead of misreporting them as broken."""
    from agents.broken_link_crawler import _url_is_disallowed

    assert _url_is_disallowed("https://example.com/admin/", ["/admin/"]) is True
    assert _url_is_disallowed("https://example.com/blog/post-1", ["/admin/"]) is False
    # Empty disallow list → never disallowed
    assert _url_is_disallowed("https://example.com/anything", []) is False


# ---------------------------------------------------------------------------
# browser_agent: screenshot_only action
# ---------------------------------------------------------------------------


def test_browser_agent_screenshot_only_is_recognised_action():
    """`screenshot_only` was added so callers who want JUST a PNG don't
    get an HTML blob they have to ignore. Locks in the new enum value."""
    from agents.browser_agent import _VALID_ACTIONS

    assert "screenshot_only" in _VALID_ACTIONS
    # Existing actions stay supported (no breakage).
    assert "screenshot" in _VALID_ACTIONS
    assert "scrape" in _VALID_ACTIONS
    assert "pdf" in _VALID_ACTIONS


# ---------------------------------------------------------------------------
# load_tester: raised caps
# ---------------------------------------------------------------------------


def test_load_tester_caps_were_raised():
    """The 2026-05-20 bumps put load_tester in real load-testing
    territory rather than sanity-check territory."""
    from agents.load_tester import _MAX_CONCURRENCY, _MAX_DURATION_S, _MAX_RPS

    assert _MAX_CONCURRENCY >= 50
    assert _MAX_DURATION_S >= 120
    assert _MAX_RPS >= 100


# ---------------------------------------------------------------------------
# stripe_webhook_debugger: aztea://echo demo path
# ---------------------------------------------------------------------------


def test_stripe_echo_demo_signs_and_verifies_in_process():
    """A valid HMAC over the payload should produce status 200 from
    _echo_verify; a tampered signature → 400."""
    from agents.stripe_webhook_debugger import _echo_verify, _make_stripe_signature

    secret = "whsec_test_demo_key"
    payload_bytes = b'{"id":"evt_1","type":"payment_intent.succeeded"}'
    now_ts = 1700000000
    valid_sig = _make_stripe_signature(payload_bytes, secret, now_ts)
    assert _echo_verify(payload_bytes, valid_sig, secret) == 200

    # Tampered signature header → 400.
    bad_sig = valid_sig.replace("v1=", "v1=ff")
    assert _echo_verify(payload_bytes, bad_sig, secret) == 400


def test_stripe_echo_sentinel_recognised_by_url_validator():
    """The endpoint URL `aztea://echo` bypasses SSRF entirely (no
    outbound HTTP happens)."""
    from agents.stripe_webhook_debugger import _validate_endpoint_url, _ECHO_SENTINEL

    assert _validate_endpoint_url(_ECHO_SENTINEL) == _ECHO_SENTINEL


# ---------------------------------------------------------------------------
# pdf_document_parser: table_extraction audit block
# ---------------------------------------------------------------------------


def test_pdf_extract_tables_returns_attempt_metadata():
    """Even when tables are empty, callers see whether the agent looked
    and what extractor it tried."""
    from agents.pdf_document_parser import _extract_tables

    # Garbage bytes — pdfplumber will fail to open. We just want the
    # attempt block populated.
    tables, attempt = _extract_tables(b"not a real pdf", max_pages=5)
    assert tables == []
    assert attempt["attempted"] is True
    assert attempt["extractor"] == "pdfplumber"
    assert "tables_found" in attempt
    assert "error" in attempt


# ---------------------------------------------------------------------------
# terraform_plan_analyzer: passed_input_fragment on parse error
# ---------------------------------------------------------------------------


def test_terraform_invalid_json_envelope_includes_fragment():
    """Locks in the new `details.passed_input_fragment` field so callers
    can see which fragment of their input was bad."""
    from agents.terraform_plan_analyzer import _parse_input

    parsed, err = _parse_input({"plan_json": "{ this is not valid json"})
    assert parsed is None
    assert err is not None
    assert err["error"]["code"] == "terraform_plan_analyzer.invalid_json"
    assert "details" in err["error"]
    assert "passed_input_fragment" in err["error"]["details"]


# ---------------------------------------------------------------------------
# k8s_manifest_validator: passed_input_fragment on YAML parse error
# ---------------------------------------------------------------------------


def test_k8s_invalid_yaml_envelope_includes_fragment():
    """Same as terraform but for YAML."""
    from agents.k8s_manifest_validator import _parse_yaml

    docs, err = _parse_yaml("kind: Pod\nmetadata: { name: x\ninvalid yaml: : :")
    assert docs is None
    assert err is not None
    assert err["error"]["code"] == "k8s_manifest_validator.invalid_yaml"
    assert "details" in err["error"]
    assert "passed_input_fragment" in err["error"]["details"]


# ---------------------------------------------------------------------------
# live_sandbox: network default-allowlist for git source
# ---------------------------------------------------------------------------


def test_live_sandbox_git_source_default_egress_is_allowlist():
    """Callers who clone a git repo with no explicit egress policy now
    get a curated allowlist instead of fully isolated. Locks in the
    new default."""
    from core.sandbox.network import build_network_argv

    # No network_cfg, source.kind="git" → allowlist mode, with github.com etc.
    argv, resolved = build_network_argv("sb_test", None, source_kind="git")
    assert resolved["egress"] == "allowlist"
    # The default allowlist includes github.com (resolved → extra_hosts);
    # if DNS resolution fails in test env, the list might be empty but
    # the policy is still 'allowlist' rather than 'isolated'.


def test_live_sandbox_explicit_isolated_still_isolated_for_git():
    """An explicit `egress: isolated` is honoured even on a git source —
    the default-allowlist only triggers when no opinion was passed."""
    from core.sandbox.network import build_network_argv

    argv, resolved = build_network_argv(
        "sb_test", {"egress": "isolated"}, source_kind="git",
    )
    assert resolved["egress"] == "isolated"
    assert argv == ["--network", "none"]


def test_live_sandbox_default_egress_for_raw_files_is_still_isolated():
    """raw_files / tarball sources don't trigger the default-allowlist —
    that's specifically about git repos that need to fetch deps."""
    from core.sandbox.network import build_network_argv

    argv, resolved = build_network_argv("sb_test", None, source_kind="raw_files")
    assert resolved["egress"] == "isolated"


# ---------------------------------------------------------------------------
# lighthouse_auditor: chromium_unavailable error code on the failure path
# ---------------------------------------------------------------------------


def test_lighthouse_chromium_unavailable_error_code_exists_in_module():
    """The chromium_unavailable structured envelope is on the post-run
    failure path (not a pre-flight short-circuit, which would break
    tests that mock subprocess.run)."""
    import agents.lighthouse_auditor as la

    src = open(la.__file__).read()
    assert "lighthouse_auditor.chromium_unavailable" in src


# ---------------------------------------------------------------------------
# python_executor: specific-pattern hint in blocked_unsafe_code envelope
# ---------------------------------------------------------------------------


def test_python_executor_blocked_error_quotes_offending_pattern():
    """Pre-fix the error said only "disallowed operations" — opaque.
    Now it names which pattern matched so the caller knows what to change."""
    from agents.python_executor import run

    result = run({"code": "import socket\nprint(1)"})
    assert "error" in result
    err = result["error"]
    assert err["code"] == "python_executor.blocked_unsafe_code"
    # Specific pattern in the message text and in details.
    assert "socket" in err["message"]
    assert err.get("details", {}).get("triggered_by") == "import socket"


def test_python_executor_safe_code_unaffected():
    """The new diagnostic helper doesn't trip on plain code."""
    from agents.python_executor import _is_safe, _first_blocked_pattern

    assert _is_safe("print(1 + 1)") is True
    assert _first_blocked_pattern("print(1 + 1)") is None


# ---------------------------------------------------------------------------
# dns_inspector: per-call DNS cache
# ---------------------------------------------------------------------------


def test_dns_inspector_caches_within_a_call():
    """Two consecutive lookups for the same host within one call must
    return the SAME list object (cached) instead of re-hitting the
    resolver."""
    from agents.dns_inspector import _cached_getaddrinfo, _reset_dns_cache

    _reset_dns_cache()
    first = _cached_getaddrinfo("localhost")
    second = _cached_getaddrinfo("localhost")
    assert first is second


def test_dns_inspector_cache_resets_per_call():
    """``_reset_dns_cache`` clears the cache so two independent run()
    invocations don't share state."""
    from agents.dns_inspector import _cached_getaddrinfo, _reset_dns_cache

    _reset_dns_cache()
    first = _cached_getaddrinfo("localhost")
    _reset_dns_cache()
    second = _cached_getaddrinfo("localhost")
    # New cache → new list object (even if contents are equal).
    assert first is not second
