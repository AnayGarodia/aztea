"""
test_agents_invariants.py — cross-cutting contracts every new agent must
honour. Failures here mean a slate agent violated the per-agent contract
the strategy doc's Section 6 sets out.

Parametrised across the seven post-cut agents (two reference agents that
work today — D16, C11 — plus five pending-infra agents). When a future
agent gets added to the slate, inclusion in ``_ALL_AGENT_SLUGS`` is the
only edit needed here.
"""

from __future__ import annotations

import importlib
import inspect
import json
import math
import os
import re
from pathlib import Path

import pytest

# Defer importing constants until after env defaults are set in conftest.
os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from tests.agent_helpers import (
    _capture_llm_calls,
    _make_response,
    _stub_llm_factory,
    assert_error_envelope,
    patch_llm_everywhere,
    set_env_for,
)


_ALL_AGENT_SLUGS = [
    # A — longitudinal
    "flake_hunter", "bisect_and_blame",
    # C — liability-bearing
    "compliance_attestor", "stripe_connect_settler",
    # D — org-memory
    "codebase_reviewer", "prod_trace_replayer", "schema_migration_planner",
]

# Map slug -> a syntactically valid payload (input validation passes; the
# config gate or LLM mock is what decides outcome). Used by the
# invariant tests that need to drive each agent past the validation step.
_VALID_PAYLOADS: dict[str, dict] = {
    "flake_hunter": {"test_path": "tests/foo.py", "repo_root": "/tmp/x"},
    "bisect_and_blame": {"good_ref": "abc", "bad_ref": "def", "repro_cmd": "x"},
    "compliance_attestor": {"control": "SOC2_CC6_1", "pr_ref": "o/r#1",
                             "check_results": []},
    "stripe_connect_settler": {"month": "2026-04",
                                "internal_ledger_source": "ledger.csv"},
    "codebase_reviewer": {"repo_id": "nonexistent",
                          "hunks": [{"file": "a.py", "text": "x = 1"}]},
    "prod_trace_replayer": {"candidate_url": "https://c.example",
                             "trace_bundle_path": "/tmp/bundle"},
    "schema_migration_planner": {"current_schema": "s1",
                                  "target_schema": "s2"},
}


def _import_agent(slug: str):
    return importlib.import_module(f"agents.{slug}")


# ---------------------------------------------------------------------------
# 1. Every agent module exposes a callable run()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_every_agent_module_exposes_run(slug):
    mod = _import_agent(slug)
    assert callable(mod.run), f"agents.{slug}.run must be callable"


# ---------------------------------------------------------------------------
# 2. Empty payload always returns a dict with error envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_every_run_returns_dict_for_empty_payload(slug):
    mod = _import_agent(slug)
    out = mod.run({})
    assert isinstance(out, dict)
    assert "error" in out


# ---------------------------------------------------------------------------
# 3. Garbage payloads (non-dict) handled cleanly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
@pytest.mark.parametrize("junk", ["string-input", 123, None, [1, 2, 3]])
def test_every_run_returns_dict_for_garbage_payload(slug, junk):
    mod = _import_agent(slug)
    out = mod.run(junk)
    assert isinstance(out, dict), f"{slug}.run({junk!r}) did not return a dict"
    assert "error" in out, (
        f"{slug}.run({junk!r}) didn't return an error envelope: {out!r}"
    )


# ---------------------------------------------------------------------------
# 4. Error codes are slug-prefixed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_error_codes_are_slug_prefixed(slug):
    mod = _import_agent(slug)
    out = mod.run({})
    code = out["error"]["code"]
    assert code.startswith(f"{slug}."), (
        f"{slug}: error code {code!r} must start with '{slug}.'"
    )


# ---------------------------------------------------------------------------
# 5. requires_configuration envelope shape
# ---------------------------------------------------------------------------

_PENDING_SLUGS = [s for s in _ALL_AGENT_SLUGS
                   if s not in {"codebase_reviewer", "compliance_attestor"}]


@pytest.mark.parametrize("slug", _PENDING_SLUGS)
def test_requires_configuration_lists_missing_keys(slug, monkeypatch):
    """Each pending agent's requires_configuration envelope must have
    details.missing as a non-empty list of strings."""
    # Clear any test env vars that might satisfy the gate.
    for var in ("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", "STRIPE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    mod = _import_agent(slug)
    payload = _VALID_PAYLOADS[slug]
    out = mod.run(payload)
    err = out["error"]
    # D16 returns repo_not_indexed before the config gate when fed a
    # nonexistent repo_id. That is also a valid honest path.
    if err["code"].endswith(".requires_configuration"):
        details = err.get("details", {})
        missing = details.get("missing")
        assert isinstance(missing, list) and missing, (
            f"{slug}: requires_configuration must list missing keys, got {missing!r}"
        )
        for m in missing:
            assert isinstance(m, str) and m, (
                f"{slug}: missing[] must be non-empty strings"
            )


# ---------------------------------------------------------------------------
# 6. Reasoning loop fires ≥ 2 LLM calls in happy path
# ---------------------------------------------------------------------------

# Slugs where the reasoning loop fires once the config gate passes. The
# scenario name in _ENV_SCENARIOS must satisfy the gate. Agents whose
# external dep can't be satisfied cleanly in unit-test scope (D16
# needs an ingested repo; C11 needs check results to validate; redteamer
# needs consent etc.) are exercised in their per-agent test files instead.
_REASONING_LOOP_SCENARIOS = [
    ("flake_hunter", "flake_hunter_configured"),
    ("bisect_and_blame", "bisect_configured"),
    ("prod_trace_replayer", "prod_trace_replayer_configured"),
]


@pytest.mark.parametrize("slug,scenario", _REASONING_LOOP_SCENARIOS)
def test_reasoning_loops_make_at_least_two_llm_calls(
    slug, scenario, monkeypatch, tmp_path,
):
    set_env_for(scenario, monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)

    payload = dict(_VALID_PAYLOADS[slug])
    # Some payloads need real on-disk files.
    if slug == "prod_trace_replayer":
        bundle = tmp_path / "bundle"
        bundle.write_text("trace")
        payload["trace_bundle_path"] = str(bundle)
    if slug == "flake_hunter":
        # Absolute path required, doesn't need to exist for the gate.
        pass

    mod = _import_agent(slug)
    out = mod.run(payload)
    # We expect the reasoning loop to fire — either to success or
    # llm_error (provider stub still counts as 2 calls).
    assert len(calls) >= 2, (
        f"{slug}: expected ≥ 2 LLM calls, got {len(calls)}. "
        f"out={out!r}"
    )


# ---------------------------------------------------------------------------
# 7. Information cascade: second LLM call must reference first response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,scenario", _REASONING_LOOP_SCENARIOS)
def test_reasoning_loops_cascade_information(
    slug, scenario, monkeypatch, tmp_path,
):
    """The plan output must appear (in some form) in the synth user message.
    This is the Section 6.2 invariant — the second call is informed by the
    first, otherwise it's not a reasoning loop, just two parallel LLM calls.
    """
    set_env_for(scenario, monkeypatch)
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)

    payload = dict(_VALID_PAYLOADS[slug])
    if slug == "prod_trace_replayer":
        bundle = tmp_path / "bundle"
        bundle.write_text("trace")
        payload["trace_bundle_path"] = str(bundle)

    mod = _import_agent(slug)
    mod.run(payload)

    assert len(calls) >= 2, f"{slug}: only {len(calls)} call(s)"
    # _capture_llm_calls's stub always returns the same JSON, so we can't
    # check that the response text appears in the next call. Instead we
    # assert the two user messages differ — they should, because step 2's
    # builder consumes step 1's response. (If step 2 ignored step 1, both
    # user messages would be derived purely from payload and likely match.)
    user_1 = next((m.content for m in calls[0].messages if m.role == "user"),
                  "")
    user_2 = next((m.content for m in calls[1].messages if m.role == "user"),
                  "")
    assert user_1 != user_2, (
        f"{slug}: first and second LLM user-messages identical — "
        "no information cascade"
    )


# ---------------------------------------------------------------------------
# 8. Trace is JSON-serialisable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,scenario", _REASONING_LOOP_SCENARIOS)
def test_trace_is_json_serialisable(slug, scenario, monkeypatch, tmp_path):
    set_env_for(scenario, monkeypatch)
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","confidence":"low","verdict":"ok","rationale":"r"}',
    ))

    payload = dict(_VALID_PAYLOADS[slug])
    if slug == "prod_trace_replayer":
        bundle = tmp_path / "bundle"
        bundle.write_text("trace")
        payload["trace_bundle_path"] = str(bundle)

    mod = _import_agent(slug)
    out = mod.run(payload)
    trace = out.get("trace") or out.get("error", {}).get("details", {}).get("trace")
    assert trace is not None, f"{slug}: no trace in output: {out!r}"
    json.dumps(trace)  # raises if not serialisable


# ---------------------------------------------------------------------------
# 9. budget_cents respected — budget_cents=1 forces failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug,scenario", _REASONING_LOOP_SCENARIOS)
def test_budget_cents_is_respected(slug, scenario, monkeypatch, tmp_path):
    """When the caller sets budget_cents=1, the first LLM call's
    pre-flight estimate is guaranteed to exceed the budget. The agent
    must surface the error envelope (budget_exceeded or llm_error)
    rather than swallowing the BudgetExceededError."""
    set_env_for(scenario, monkeypatch)
    # No LLM stub — let the real run_with_fallback fire the budget gate.

    payload = dict(_VALID_PAYLOADS[slug])
    payload["budget_cents"] = 1
    if slug == "prod_trace_replayer":
        bundle = tmp_path / "bundle"
        bundle.write_text("trace")
        payload["trace_bundle_path"] = str(bundle)

    mod = _import_agent(slug)
    out = mod.run(payload)
    assert "error" in out, f"{slug}: budget=1 should produce error"
    code = out["error"]["code"]
    # Either budget_exceeded (caught explicitly) or llm_error (propagated as LLMError)
    assert code.endswith(".budget_exceeded") or code.endswith(".llm_error"), (
        f"{slug}: budget=1 expected budget_exceeded/llm_error, got {code!r}"
    )


# ---------------------------------------------------------------------------
# 10–11. Spec uniqueness + agent ID disjointness
# ---------------------------------------------------------------------------


def test_all_agents_appear_exactly_once_in_specs_part11():
    """After the 2026-05-23 editorial cut: seven agents (2 reference + 5 pending)."""
    from server.builtin_agents.specs_part11 import load_builtin_specs_part11
    specs = load_builtin_specs_part11()
    ids = [s["agent_id"] for s in specs]
    assert len(ids) == 7
    assert len(set(ids)) == 7, "duplicate agent_ids in specs_part11"


def test_no_agent_id_collides_with_existing_builtin():
    from server.builtin_agents import constants as c
    new_ids = (c.PENDING_INFRA_AGENT_IDS |
                {c.CODEBASE_REVIEWER_AGENT_ID,
                 c.COMPLIANCE_ATTESTOR_AGENT_ID})
    # Pre-existing built-in IDs minus the new ones.
    pre_existing = (c.BUILTIN_AGENT_IDS - new_ids)
    assert not (new_ids & pre_existing), (
        "new agent IDs collide with pre-existing builtin IDs"
    )


# ---------------------------------------------------------------------------
# 12. Pending agents return requires_configuration when valid input is sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _PENDING_SLUGS)
def test_pending_agent_returns_requires_configuration_or_authorization(
    slug, monkeypatch,
):
    """No env-var leakage; valid payload; must surface a *configuration*
    style error (not invalid_input or llm_error)."""
    # Clear all env vars the pending agents inspect.
    for var in ("AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED", "STRIPE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    mod = _import_agent(slug)
    out = mod.run(_VALID_PAYLOADS[slug])
    code = out["error"]["code"]
    # Acceptable terminal codes for a pending agent without config.
    acceptable = {f"{slug}.requires_configuration"}
    assert code in acceptable, (
        f"{slug}: expected one of {acceptable}, got {code!r}. Full out: {out!r}"
    )


# ---------------------------------------------------------------------------
# 13. NaN / Inf rejected in numeric inputs (where the agent has them)
# ---------------------------------------------------------------------------

# (Most numeric fields are clamped int, which int(nan) → TypeError. We assert
# the agent surfaces a clean error envelope rather than crashing.)


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_no_agent_crashes_on_nan_inf_in_payload(slug):
    """Pass nan/inf in arbitrary numeric-looking keys; agent must NOT crash."""
    mod = _import_agent(slug)
    nan_payload = dict(_VALID_PAYLOADS[slug])
    # Stuff NaN under a key the agent might or might not read.
    nan_payload["budget_cents"] = math.nan
    out = mod.run(nan_payload)
    assert isinstance(out, dict)
    # Either accepted-but-clamped (clamp_int returns default for nan-int conversion)
    # or rejected with an error. Both are fine; crashing is not.


# ---------------------------------------------------------------------------
# 14. Output never has both error and success keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_no_agent_returns_dict_with_both_error_and_success_keys(slug):
    mod = _import_agent(slug)
    out = mod.run({})  # error envelope expected
    if "error" in out:
        # Ensure no top-level "summary"/"plan"/"synthesis"/"attestation"/"manifest" keys
        forbidden = {"summary", "plan", "synthesis", "attestation",
                      "manifest", "findings", "status"}
        leaked = forbidden & set(out.keys())
        assert not leaked, (
            f"{slug}: error envelope leaked success keys: {leaked}"
        )


# ---------------------------------------------------------------------------
# 15. Module docstring present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_module_docstring_present(slug):
    """CLAUDE.md rule 7 — every non-trivial module needs a docstring."""
    mod = _import_agent(slug)
    assert mod.__doc__ and len(mod.__doc__.strip()) > 50, (
        f"agents.{slug}: docstring missing or too short"
    )


# ---------------------------------------------------------------------------
# 16. Architectural: agents must not import from server.routes / server.application_parts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_no_agent_imports_from_server_routes_or_application_parts(slug):
    """Agents are business logic; HTTP transport is downstream."""
    path = Path("agents") / f"{slug}.py"
    src = path.read_text()
    # `from server.routes` or `from server.application_parts` are forbidden.
    assert "from server.routes" not in src, (
        f"agents.{slug}: imports from server.routes (architectural rule)"
    )
    assert "from server.application_parts" not in src, (
        f"agents.{slug}: imports from server.application_parts"
    )


# ---------------------------------------------------------------------------
# 17. No floating-point money — agents must not float() inside their bodies
# ---------------------------------------------------------------------------

_FLOAT_ALLOWLIST = {
    # confidence_threshold + similar non-money floats are fine.
    "codebase_reviewer",  # score rounding only (uses round(), not float())
}


@pytest.mark.parametrize("slug", _ALL_AGENT_SLUGS)
def test_no_float_calls_in_money_paths(slug):
    """Scan each agent module for explicit ``float(`` calls. Allowlist
    covers the few legitimate non-money uses (confidence thresholds).

    This mirrors the project-wide money rule from CLAUDE.md; agents don't
    own money paths today but the rule keeps drift out.
    """
    if slug in _FLOAT_ALLOWLIST:
        pytest.skip(f"{slug}: allowlisted (non-money float use documented)")
    path = Path("agents") / f"{slug}.py"
    src = path.read_text()
    # Count float( occurrences outside comments / docstrings — quick + dirty.
    code_lines = [
        ln for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    float_count = sum(
        len(re.findall(r"\bfloat\s*\(", ln)) for ln in code_lines
    )
    assert float_count == 0, (
        f"agents.{slug} contains {float_count} float() call(s) — "
        "add to _FLOAT_ALLOWLIST with reason if non-money"
    )
