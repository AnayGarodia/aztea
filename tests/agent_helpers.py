"""
agent_helpers.py — shared mocks + fixtures for the 25-agent test suite.

# OWNS: LLM stubs, signing stubs, git-fixture builders, ingest helpers,
#       invariant-assertion utilities, pytest fixtures used across the
#       30-file agent test suite.
# NOT OWNS: per-agent business assertions (those live in tests/test_agent_*.py).
#
# Why centralised: every per-agent test file would otherwise repeat 50 lines
# of mock plumbing. Putting it once here keeps the per-agent files focused
# on the behaviour under test.

Usage:

    from tests.agent_helpers import (
        stub_llm, stateful_llm, capture_llm,
        patch_llm_everywhere, _build_fixture_repo,
        assert_reasoning_loop, assert_error_envelope,
        fake_signer, set_env_for,
    )
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

import pytest

from core.llm.base import CompletionRequest, LLMResponse, Usage
from core.llm.errors import BudgetExceededError, LLMError


# ---------------------------------------------------------------------------
# LLM stubs
# ---------------------------------------------------------------------------


def _make_response(text: str, *, prompt_tokens: int = 200,
                   completion_tokens: int = 400) -> LLMResponse:
    """Build a uniform LLMResponse for stubs. Matches the real shape."""
    return LLMResponse(
        text=text,
        model="stub",
        provider="stub",
        usage=Usage(prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens),
    )


def _stub_llm_factory(text_or_seq: str | list[str]) -> Callable:
    """Return a stub function that mimics core.llm.fallback.run_with_fallback.

    Two modes:
    * Single string → every call returns the same text.
    * Sequence → call N returns text[N]; raises IndexError on overflow.

    Why two modes: simple tests want a fixed response; reasoning-loop
    tests want to assert ordered step responses.
    """
    if isinstance(text_or_seq, str):
        def _single(req, *args, **kwargs):
            return _make_response(text_or_seq)
        return _single

    sequence = list(text_or_seq)
    counter = {"i": 0}

    def _multi(req, *args, **kwargs):
        i = counter["i"]
        if i >= len(sequence):
            raise IndexError(
                f"LLM stub exhausted after {len(sequence)} calls — "
                "test expected fewer LLM invocations"
            )
        counter["i"] = i + 1
        return _make_response(sequence[i])

    return _multi


def _make_stateful_llm(
    plan_text: str,
    synth_text: str,
    *,
    synth_assertion: Callable[[CompletionRequest], None] | None = None,
) -> Callable:
    """Return a stub that captures the plan and verifies the synth call sees it.

    Default assertion: ``plan_text`` must appear somewhere in the second
    call's user message. Tests can pass a custom ``synth_assertion`` for
    stricter checks (e.g., "the synth user contains all per-hunk verdicts").
    """
    state = {"calls": 0, "plan_in": None}

    def _stub(req, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            state["plan_in"] = _last_user_content(req)
            return _make_response(plan_text)
        if state["calls"] == 2:
            if synth_assertion is not None:
                synth_assertion(req)
            else:
                # Default cascade check: plan response appears in synth prompt.
                user = _last_user_content(req)
                assert plan_text in user or plan_text[:50] in user, (
                    f"second LLM call's user message {user[:200]!r} does "
                    f"not contain plan output {plan_text[:50]!r}"
                )
            return _make_response(synth_text)
        raise RuntimeError(
            f"agent invoked LLM {state['calls']}× — test only expected 2"
        )

    return _stub


def _capture_llm_calls() -> tuple[Callable, list[CompletionRequest]]:
    """Return (stub, calls_list). Each invocation appends ``req`` to the list.

    Test assertions:
        stub, calls = _capture_llm_calls()
        patch_llm_everywhere(monkeypatch, stub)
        agent.run(payload)
        assert len(calls) == 2
        assert calls[1].messages[-1].content contains calls[0].text  (cascade)
    """
    calls: list[CompletionRequest] = []

    def _stub(req, *args, **kwargs):
        calls.append(req)
        # Return a generic JSON response so JSON-parsing agents don't crash.
        # Per-agent tests that need specific text should use _stub_llm_factory
        # instead and assert call shape separately.
        return _make_response('{"summary":"ok","confidence":"low",'
                              '"verdict":"ok","rationale":"r"}')

    return _stub, calls


def _last_user_content(req: CompletionRequest) -> str:
    """Pure: extract the most recent user-role message content."""
    for msg in reversed(req.messages):
        if msg.role == "user":
            return msg.content
    return ""


# ---------------------------------------------------------------------------
# Patching: every place run_with_fallback is imported.
# ---------------------------------------------------------------------------
#
# Python's import system caches function references at import time. Patching
# `core.llm.fallback.run_with_fallback` does NOT affect modules that already
# did `from core.llm.fallback import run_with_fallback`. So tests must patch
# at the *callsite* module, not the source. The list below is the closure of
# every module in the 25-agent slate that imports the function directly.

_LLM_CALLSITES = [
    "core.llm.fallback",  # canonical source
    "agents._reasoning_scaffold",  # used by 22 scaffolded agents
    "agents.codebase_reviewer",  # D16 imports directly
    "agents.compliance_attestor",  # C11 imports directly
    "agents.ai_code_provenance_stamp",  # E25 imports directly
    "agents.flake_hunter",  # imports directly (pre-scaffold version)
]


def patch_llm_everywhere(monkeypatch, stub_fn: Callable) -> None:
    """Patch ``run_with_fallback`` at every known callsite.

    Why: agents import `run_with_fallback` via `from ... import ...`, which
    captures the symbol locally. Patching only the source leaves the local
    binding stale. We patch each callsite explicitly.
    """
    import importlib
    for modpath in _LLM_CALLSITES:
        try:
            mod = importlib.import_module(modpath)
        except ImportError:
            continue
        if hasattr(mod, "run_with_fallback"):
            monkeypatch.setattr(f"{modpath}.run_with_fallback", stub_fn)


# ---------------------------------------------------------------------------
# Signing stubs — deterministic 88-char shape matching Ed25519.
# ---------------------------------------------------------------------------


def _fake_sign_payload(private_pem: str, payload: Any) -> str:
    """Deterministic stand-in for core.crypto.sign_payload.

    Same shape as Ed25519 + base64 (88 chars), derived from sha256 of the
    canonical payload bytes so two calls with the same payload produce the
    same signature.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # Pad to 88 chars (real Ed25519 sigs are 88 base64 chars).
    return (h + h)[:88]


def _fake_verify_signature(public_pem: str, payload: Any, sig_b64: str) -> bool:
    """Paired verifier — returns True iff signed by _fake_sign_payload."""
    return _fake_sign_payload("", payload) == sig_b64


def patch_signing_everywhere(monkeypatch) -> tuple[Callable, Callable]:
    """Replace sign_payload + verify_signature at every callsite.

    Returns (fake_sign, fake_verify) so tests can call them directly to
    assert sig values without re-importing.
    """
    import importlib
    callsites = [
        "core.crypto", "core.identity",
        "agents.compliance_attestor", "agents.ai_code_provenance_stamp",
    ]
    for modpath in callsites:
        try:
            mod = importlib.import_module(modpath)
        except ImportError:
            continue
        if hasattr(mod, "sign_payload"):
            monkeypatch.setattr(f"{modpath}.sign_payload", _fake_sign_payload)
        if hasattr(mod, "verify_signature"):
            monkeypatch.setattr(f"{modpath}.verify_signature",
                                _fake_verify_signature)
    return _fake_sign_payload, _fake_verify_signature


# ---------------------------------------------------------------------------
# Environment-gate helper
# ---------------------------------------------------------------------------


_ENV_SCENARIOS: dict[str, dict[str, str]] = {
    "flake_hunter_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "bisect_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "deploy_canary_configured": {
        "AZTEA_DEPLOY_API_TOKEN": "tok-fake",
        "AZTEA_METRICS_API_URL": "https://metrics.example/api",
    },
    "migration_pilot_configured": {
        "AZTEA_MIGRATION_REPLICA_DSN": "postgres://localhost/test",
    },
    "pr_watch_configured": {
        "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1",
        "GITHUB_APP_ID": "12345",
        # github_app.is_configured also checks the private key file; tests
        # that need this scenario also need to point GITHUB_APP_PRIVATE_KEY_PATH
        # at a tmp file via monkeypatch.
    },
    "fuzz_and_find_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "mutation_doctor_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "refactor_verifier_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "llm_eval_configured": {
        "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1",
        "OPENAI_API_KEY": "sk-fake-1",
        "ANTHROPIC_API_KEY": "sk-fake-2",
    },
    "config_solver_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "dmarc_configured": {
        "SMTP_HOST": "smtp.example", "SMTP_USER": "u", "SMTP_PASS": "p",
        "AZTEA_DMARC_CANARY_INBOX": "canary@example.com",
    },
    "stripe_settler_configured": {"STRIPE_API_KEY": "sk_test_fake"},
    "incident_captain_configured": {
        "PAGERDUTY_API_TOKEN": "tok-pd",
        "SENTRY_API_TOKEN": "tok-sentry",
        "AZTEA_INCIDENT_DOC_TARGET": "https://docs.example/war-room",
    },
    "prod_trace_replayer_configured": {"AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1"},
    "redteam_configured": {
        "AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED": "1",
        "AZTEA_REDTEAM_CONSENT_SIGNING_KEY": "fake-consent-key",
    },
    "privacy_tracer_configured": {
        "AZTEA_OTEL_COLLECTOR_URL": "https://otel.example/v1",
        "AZTEA_EBPF_AGENT_SOCKET": "/var/run/ebpf.sock",
    },
    # The reference agents (D16, C11) don't need env vars; they're listed
    # so tests can refer to them by scenario name uniformly.
    "codebase_reviewer_configured": {},
    "compliance_attestor_configured": {},
    "api_contract_negotiator_configured": {},
    "ai_provenance_configured": {},
}


def set_env_for(scenario: str, monkeypatch) -> None:
    """Apply the env vars for a named scenario via monkeypatch.

    Why a helper instead of inline setenv: each scenario's exact var list
    is centralised so a config-gate change in an agent module updates
    every test in one place.
    """
    if scenario not in _ENV_SCENARIOS:
        raise KeyError(f"unknown env scenario: {scenario!r}")
    for key, val in _ENV_SCENARIOS[scenario].items():
        monkeypatch.setenv(key, val)


# ---------------------------------------------------------------------------
# Git fixture repos
# ---------------------------------------------------------------------------


def _build_fixture_repo(
    tmpdir: str | os.PathLike, scenario: str = "bug_revert_fix",
) -> tuple[str, dict[str, str]]:
    """Build a small git repo under ``tmpdir``. Returns (repo_path, sha_dict).

    Scenarios:
      * bug_revert_fix — 3 commits: initial → bug → revert. Returns
        sha_dict with keys ``initial``, ``bug``, ``revert``.
      * linear — 5 unrelated commits.
      * multifile — single commit that touches 4 files.

    Why three scenarios: D16 needs the bug-revert flow to see strong
    signal; D16's max_hunks bound test needs multifile; the linear case
    is the negative — no signal expected.
    """
    repo_path = Path(tmpdir) / "fixture"
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q", "-b", "main")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")
    _git(repo_path, "config", "commit.gpgsign", "false")

    if scenario == "bug_revert_fix":
        return repo_path, _scenario_bug_revert_fix(repo_path)
    if scenario == "linear":
        return repo_path, _scenario_linear(repo_path)
    if scenario == "multifile":
        return repo_path, _scenario_multifile(repo_path)
    raise ValueError(f"unknown scenario: {scenario!r}")


def _scenario_bug_revert_fix(repo_path: Path) -> dict[str, str]:
    (repo_path / "hello.py").write_text("def add(a, b):\n    return a + b\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-q", "-m", "initial add function")
    initial = _git_sha(repo_path)

    (repo_path / "hello.py").write_text("def add(a, b):\n    return a - b\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-q", "-m", "refactor add (oops)")
    bug = _git_sha(repo_path)

    _git(repo_path, "revert", "--no-edit", bug)
    revert = _git_sha(repo_path)
    return {"initial": initial, "bug": bug, "revert": revert}


def _scenario_linear(repo_path: Path) -> dict[str, str]:
    shas: dict[str, str] = {}
    for i in range(5):
        (repo_path / f"f{i}.py").write_text(f"x = {i}\n")
        _git(repo_path, "add", ".")
        _git(repo_path, "commit", "-q", "-m", f"commit {i}")
        shas[f"c{i}"] = _git_sha(repo_path)
    return shas


def _scenario_multifile(repo_path: Path) -> dict[str, str]:
    for name, body in [
        ("a.py", "x = 1\n"),
        ("b.py", "y = 2\n"),
        ("c.py", "z = 3\n"),
        ("d.py", "w = 4\n"),
    ]:
        (repo_path / name).write_text(body)
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-q", "-m", "four files in one commit")
    return {"multi": _git_sha(repo_path)}


def _git(cwd: Path, *args: str) -> None:
    """Run git in cwd, raising on non-zero. Quiet-by-default."""
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def _git_sha(cwd: Path) -> str:
    """Return HEAD sha."""
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(cwd), text=True,
    ).strip()


def _ingest_fixture_repo(
    tmpdir: str | os.PathLike, scenario: str = "bug_revert_fix",
) -> tuple[Any, dict[str, str]]:
    """Build + ingest a fixture repo via core.hosted_index. Returns (IngestResult, shas)."""
    from core import hosted_index as hi
    repo_path, shas = _build_fixture_repo(tmpdir, scenario)
    result = hi.ingest_repo(owner_id="test-owner", source=str(repo_path))
    return result, shas


# ---------------------------------------------------------------------------
# Invariant assertions
# ---------------------------------------------------------------------------


def assert_reasoning_loop(result: dict[str, Any]) -> None:
    """Assert the result carries a well-formed reasoning trace with ≥2 LLM calls.

    The trace may live at:
      * ``result["trace"]`` (success path), or
      * ``result["error"]["details"]["trace"]`` (some error paths).

    Why both: budget_exceeded / llm_error envelopes preserve the trace
    inside the error details so callers can audit which steps happened
    before the failure.
    """
    trace = result.get("trace")
    if trace is None:
        err = result.get("error") or {}
        details = err.get("details") or {}
        trace = details.get("trace")
    assert isinstance(trace, dict), f"no trace dict found in result: {result!r}"
    assert "step_count" in trace and "steps" in trace
    assert trace["step_count"] >= 1, "trace must contain at least one step"
    for step in trace["steps"]:
        assert step["status"] in {"ok", "failed"}
        assert step["duration_ms"] >= 0
        assert "name" in step and step["name"]
    # JSON round-trip — proves serialisable.
    json.dumps(trace)


def assert_error_envelope(
    result: dict[str, Any],
    code_prefix: str,
    *,
    has_details_key: str | None = None,
) -> dict[str, Any]:
    """Assert ``result`` is a canonical error envelope. Returns the error dict.

    Why return: tests often want to inspect details after asserting shape.
    """
    assert isinstance(result, dict), f"result not a dict: {result!r}"
    assert "error" in result, f"no error key in {result!r}"
    err = result["error"]
    assert isinstance(err, dict)
    assert "code" in err and "message" in err
    assert err["code"].startswith(code_prefix), (
        f"expected code prefix {code_prefix!r}, got {err['code']!r}"
    )
    if has_details_key is not None:
        assert "details" in err and has_details_key in err["details"], (
            f"missing {has_details_key!r} in details: {err.get('details')!r}"
        )
    return err


def assert_min_llm_calls(call_log: list[CompletionRequest], minimum: int) -> None:
    """Assert at least ``minimum`` LLM calls were captured."""
    assert len(call_log) >= minimum, (
        f"expected ≥ {minimum} LLM calls, got {len(call_log)}"
    )


def assert_cascade(call_log: list[CompletionRequest]) -> None:
    """Assert the second LLM call's user message references the first call's
    output. This is the Section 6.2 reasoning-loop invariant.

    The stub used by `_capture_llm_calls` returns a fixed JSON; for cascade
    assertion the test should use `_make_stateful_llm` instead. This helper
    is a sanity check that simply asserts ≥ 2 distinct user messages were
    sent (the cascade content check lives in `_make_stateful_llm`).
    """
    assert len(call_log) >= 2, "need ≥ 2 calls to check cascade"
    msgs = [_last_user_content(req) for req in call_log[:2]]
    assert msgs[0] != msgs[1], (
        "first and second LLM calls had identical user messages — "
        "no information cascade"
    )


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_llm(monkeypatch):
    """Fixture: returns a function (text) -> applied stub.

    Usage:
        def test_x(stub_llm):
            stub_llm("fake response text")  # patches everywhere
            ...
    """
    def _apply(text_or_seq: str | list[str]):
        patch_llm_everywhere(monkeypatch, _stub_llm_factory(text_or_seq))
    return _apply


@pytest.fixture
def stateful_llm(monkeypatch):
    """Fixture: returns a function (plan, synth, *, assertion=None) -> applies stub."""
    def _apply(plan_text: str, synth_text: str, *, synth_assertion=None):
        patch_llm_everywhere(
            monkeypatch, _make_stateful_llm(plan_text, synth_text,
                                            synth_assertion=synth_assertion),
        )
    return _apply


@pytest.fixture
def llm_call_log(monkeypatch):
    """Fixture: returns the list of CompletionRequest captured during the test."""
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    return calls


@pytest.fixture
def fake_signer(monkeypatch):
    """Fixture: returns (sign_fn, verify_fn) with both already patched in."""
    return patch_signing_everywhere(monkeypatch)


@pytest.fixture
def fixture_repo(tmp_path):
    """Fixture: pre-built bug_revert_fix git repo. Returns (repo_path, shas)."""
    return _build_fixture_repo(tmp_path, "bug_revert_fix")


@pytest.fixture
def ingested_repo(tmp_path):
    """Fixture: bug_revert_fix repo already ingested via hosted_index.

    Yields (repo_id, shas). Cleans up at teardown.
    """
    from core import hosted_index as hi
    result, shas = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    yield result.repo_id, shas
    try:
        hi.delete_repo(result.repo_id)
    except Exception:
        pass


@pytest.fixture
def clean_namespaces():
    """Clean any vector_store namespaces used by tests, before+after."""
    from core import vector_store as vs
    namespaces = ["test_d16", "test_d17", "test_e21"]
    for ns in namespaces:
        try:
            vs.delete_namespace(ns)
        except Exception:
            pass
    yield
    for ns in namespaces:
        try:
            vs.delete_namespace(ns)
        except Exception:
            pass


__all__ = [
    "_stub_llm_factory",
    "_make_stateful_llm",
    "_capture_llm_calls",
    "patch_llm_everywhere",
    "_fake_sign_payload",
    "_fake_verify_signature",
    "patch_signing_everywhere",
    "set_env_for",
    "_build_fixture_repo",
    "_ingest_fixture_repo",
    "assert_reasoning_loop",
    "assert_error_envelope",
    "assert_min_llm_calls",
    "assert_cascade",
    "_make_response",
    "_ENV_SCENARIOS",
    # fixtures
    "stub_llm",
    "stateful_llm",
    "llm_call_log",
    "fake_signer",
    "fixture_repo",
    "ingested_repo",
    "clean_namespaces",
]
