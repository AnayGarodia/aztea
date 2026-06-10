"""Regression tests for the second-pass 2026-05-08 rails work.

Plan: /Users/aakritigarodia/.claude/plans/supply-is-fine-i-steady-wind.md

Each test pins one fix from that plan. Failures mean the eval condition has
resurfaced — fix the underlying code, do not weaken the test.

Fixes covered:
  R1 — CVE Lookup empty {} payload returns structured 422 (was 502)
  R2 — Python explainer skipped when run timed out
  R3 — DB Sandbox refunds when every SQL statement errored
  R4 — MCP local search no longer baselines registry agents above the floor
  R5 — Search empty-result hint enumerates live categories from the catalog
  R6 — Dispute pre-flight is_disputable() accepts status churn post-completion
  R7 — Trust breakdown surfaced on agent responses
  R8 — list_agents augments missing curated builtins
  R9 — Audit verify_all uses a per-receipt timeout cap
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# R1 — CVE Lookup empty input → structured error envelope
# ---------------------------------------------------------------------------


def test_r1_cve_empty_input_returns_structured_error():
    """Calling cve_lookup with neither cve_id, cve_ids, nor packages must
    return an error envelope (`{"error": {...}}`) so the platform converts
    it to 422 + refund. The eval found that an empty {} returned 502 with
    empty raw_body because the agent silently fell into a no-op success
    branch and the platform mishandled `billing_units_actual: 0`.
    """
    from agents import cve_lookup

    result = cve_lookup.run({})

    assert "error" in result, (
        "Empty input must return an `error` envelope, not a success."
    )
    assert result["error"]["code"] == "cve_lookup.missing_input", (
        f"Expected code cve_lookup.missing_input, got {result['error']['code']!r}"
    )
    assert "results" not in result, (
        "Error envelope must not include success fields like 'results'."
    )


# ---------------------------------------------------------------------------
# R2 — Python explainer skipped on timeout
# ---------------------------------------------------------------------------


def test_r2_python_executor_skips_explainer_on_timeout(monkeypatch):
    """The 2026-05-08 eval saw `while True: pass` time out at exit 124 and
    THEN spend ~300ms running an LLM 'explanation' that added zero insight.
    Asserted behaviorally (the old source-regex pinned one if-statement
    shape and broke on an equivalent refactor): with explain=True and a
    timeout, the LLM must never be invoked.
    """
    from agents import python_executor

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("explainer LLM was invoked on a timed-out run (R2)")

    monkeypatch.setattr(python_executor, "run_with_fallback", _must_not_be_called)
    result = python_executor.run(
        {"code": "while True: pass", "timeout": 1, "explain": True}
    )
    assert result["timed_out"] is True
    assert result["explanation"] == ""
    assert result["explanation_status"] == "skipped_timeout"


# ---------------------------------------------------------------------------
# R3 — DB Sandbox refunds on all-error result sets
# ---------------------------------------------------------------------------


def test_r3_db_sandbox_refunds_when_all_statements_error():
    """When every SQL statement in a call errored (e.g. DROP TABLE
    sqlite_master is blocked), the response must be an error envelope so
    the platform refunds. Other agents (cve_lookup, dependency_auditor)
    already do this for their own input/internal failures; the eval
    flagged db_sandbox as the only one charging users for a guaranteed-
    block result.
    """
    from agents import db_sandbox

    result = db_sandbox.run({"sql": "DROP TABLE sqlite_master"})
    assert "error" in result, (
        f"All-statements-errored call must return error envelope, got: {result!r}"
    )
    assert result["error"]["code"] == "db_sandbox.sql_error"
    # Mixed success/error stays charged: a single ok-then-error call
    # should NOT trigger the refund path.
    mixed = db_sandbox.run(
        {
            "queries": [
                {"sql": "SELECT 1"},
                {"sql": "DROP TABLE sqlite_master"},
            ]
        }
    )
    assert "error" not in mixed, (
        "Partial success must stay charged; only all-errored refunds."
    )


# ---------------------------------------------------------------------------
# R4 — MCP local search no longer baselines registry agents
# ---------------------------------------------------------------------------


def test_r4_mcp_local_search_strips_quality_prior_baseline():
    """The 2026-05-08 eval root-caused the discovery 'no empty-result mode'
    bug to a quality-prior baseline in the MCP local lexical scorer:
    every registry agent received `success_rate*10 + trust/20 +
    stability_bonus`, trivially clearing _LOCAL_SEARCH_MIN_SCORE = 6, so
    even off-topic queries claimed a hit instead of falling through to
    the server-side semantic ranker. This test pins the deletion.
    """
    # 1.6.2 — MCP server moved into the SDK package; scripts/ now holds
    # only a compat shim. Pin the deletion in the canonical location.
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    # The deleted block was a bare quality-prior addition: any of these
    # exact lines indicates the bug has crept back.
    assert 'float(entry.get("success_rate") or 0.0) * 10.0' not in src, (
        "Quality-prior baseline regressed — registry agents must not get "
        "success_rate*10 added unconditionally to their lexical score."
    )
    assert 'float(entry.get("trust_score") or 0.0) / 20.0' not in src, (
        "Trust baseline regressed in MCP local scorer."
    )


# ---------------------------------------------------------------------------
# R5 — Search empty-result hint enumerates live categories
# ---------------------------------------------------------------------------


def test_r5_search_empty_result_hint_names_live_categories():
    """When search returns zero results, the next_step must call out the
    catalog's live categories instead of a generic 'use list_agents'
    breadcrumb. The category list must be derived from the catalog itself
    (no hardcoded list) so it stays accurate as agents are added/removed.
    """
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    # The empty-state branch must enumerate categories. We do not test
    # against a specific category set (that'd be a hardcode) — just that
    # the code reads category from each entry to assemble the hint.
    assert 'No agent in the live catalog matches this task' in src, (
        "Empty-result hint copy must signal `no match` clearly."
    )
    assert 'entry.get("category")' in src and '_live_categories' in src, (
        "Empty-result hint must derive categories from the live catalog "
        "rather than hardcode them."
    )


# ---------------------------------------------------------------------------
# R6 — is_disputable predicate
# ---------------------------------------------------------------------------


def _isoz(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def test_r6a_is_disputable_accepts_complete_within_window():
    from core.jobs import disputable

    now = datetime.now(timezone.utc)
    job = {
        "job_id": "j1",
        "status": "complete",
        "completed_at": _isoz(now - timedelta(minutes=5)),
    }
    reason = disputable.is_disputable(
        job,
        deadline=now + timedelta(hours=72),
        has_existing_dispute=False,
        has_quality_rating=False,
        now=now,
    )
    assert reason is None, (
        f"Recently-completed unrated job should be disputable; got: {reason}"
    )


def test_r6b_is_disputable_accepts_status_churn_post_completion():
    """The 2026-05-08 P1: a job whose status was 'complete' at receipt-
    issue time but had churned by the time the dispute route read it
    (sweeper, verification rejection) was rejected with 400. The new
    helper anchors on `completed_at` (durable) so that race no longer
    locks out the caller.
    """
    from core.jobs import disputable

    now = datetime.now(timezone.utc)
    job = {
        "job_id": "j2",
        "status": "failed",  # status churned after completion
        "completed_at": _isoz(now - timedelta(minutes=2)),
    }
    reason = disputable.is_disputable(
        job,
        deadline=now + timedelta(hours=72),
        has_existing_dispute=False,
        has_quality_rating=False,
        now=now,
    )
    assert reason is None, (
        "Job with completed_at set must be disputable even if its current "
        "status has churned — that's the bug R6 was created to fix."
    )


def test_r6c_is_disputable_rejects_never_completed():
    from core.jobs import disputable

    now = datetime.now(timezone.utc)
    reason = disputable.is_disputable(
        {"job_id": "j3", "status": "running", "completed_at": None},
        deadline=now + timedelta(hours=72),
        has_existing_dispute=False,
        has_quality_rating=False,
        now=now,
    )
    assert reason is not None
    assert reason.code == "dispute.not_completed"


def test_r6d_is_disputable_rejects_after_window():
    from core.jobs import disputable

    now = datetime.now(timezone.utc)
    reason = disputable.is_disputable(
        {
            "job_id": "j4",
            "status": "complete",
            "completed_at": _isoz(now - timedelta(days=4)),
        },
        deadline=now - timedelta(minutes=1),
        has_existing_dispute=False,
        has_quality_rating=False,
        now=now,
    )
    assert reason is not None
    assert reason.code == "dispute.window_closed"


def test_r6e_is_disputable_rejects_after_rating():
    from core.jobs import disputable

    now = datetime.now(timezone.utc)
    reason = disputable.is_disputable(
        {
            "job_id": "j5",
            "status": "complete",
            "completed_at": _isoz(now),
        },
        deadline=now + timedelta(hours=72),
        has_existing_dispute=False,
        has_quality_rating=True,
        now=now,
    )
    assert reason is not None
    assert reason.code == "dispute.already_rated"
    assert reason.status_code == 409


def test_r6f_update_job_status_guard_blocks_post_completion_terminal_flip(
    monkeypatch, tmp_path
):
    """Pin the invariant disputable.py DECISIONS #3 relies on.

    `update_job_status(..., completed=True)` must be a no-op once
    `completed_at` is set. If this guard ever weakens, the disputable
    predicate's anchoring on `completed_at` becomes unsafe (a settled job
    could flip from `complete` to `failed` post-payout, opening a window
    for a partial clawback when the dispute path runs against fresh state).
    """
    import uuid as _uuid
    from core import jobs as _jobs
    from core import registry as _registry

    db_path = tmp_path / f"r6f-{_uuid.uuid4().hex}.db"
    for module in (_jobs, _registry):
        conn = getattr(module._local, "conn", None)
        if conn is not None:
            conn.close()
            try:
                delattr(module._local, "conn")
            except AttributeError:
                pass
        monkeypatch.setattr(module, "DB_PATH", str(db_path))

    _registry.init_db()
    _jobs.init_jobs_db()

    agent_id = _registry.register_agent(
        name="r6f agent",
        description="guard test",
        endpoint_url="https://example.com/r6f",
        price_per_call_usd=0.01,
        tags=["r6f"],
    )
    job = _jobs.create_job(
        agent_id=agent_id,
        caller_owner_id=f"user:{_uuid.uuid4().hex[:8]}",
        caller_wallet_id=str(_uuid.uuid4()),
        agent_wallet_id=str(_uuid.uuid4()),
        platform_wallet_id=str(_uuid.uuid4()),
        price_cents=10,
        charge_tx_id=str(_uuid.uuid4()),
        input_payload={"task": "guard"},
    )

    settled = _jobs.update_job_status(
        job["job_id"], "complete", output_payload={"ok": True}, completed=True
    )
    assert settled["status"] == "complete"
    assert settled["completed_at"] is not None
    original_completed_at = settled["completed_at"]

    # Attempt a post-completion terminal flip — this is what a misbehaving
    # sweeper or buggy verification path might try. The SQL guard must
    # silently no-op (status stays `complete`, `completed_at` stays put).
    after = _jobs.update_job_status(
        job["job_id"], "failed", error_message="late failure", completed=True
    )
    assert after is not None
    assert after["status"] == "complete", (
        "Post-completion terminal flip should be a no-op; the SQL guard in "
        "core/jobs/leases.py is what makes disputable.py's completed_at "
        "anchor safe."
    )
    assert after["completed_at"] == original_completed_at

    # Cleanup so other tests don't inherit the tmp DB.
    for module in (_jobs, _registry):
        conn = getattr(module._local, "conn", None)
        if conn is not None:
            conn.close()
            try:
                delattr(module._local, "conn")
            except AttributeError:
                pass


# ---------------------------------------------------------------------------
# R7 — trust_breakdown on agent_response
# ---------------------------------------------------------------------------


def test_r7_agent_response_exposes_trust_breakdown_alias():
    src = Path("server/application_parts/part_002.py").read_text()
    # The eval's reputation-grade complaint was "no visibility into score
    # components". `trust_breakdown` is the legible alias derived from
    # the existing reputation dict; both must be present.
    assert 'out["trust_breakdown"]' in src, (
        "_agent_response must expose trust_breakdown so callers can see "
        "WHY a score is what it is (eval finding R7)."
    )


# ---------------------------------------------------------------------------
# R8 — list_agents parity with curated public builtins
# ---------------------------------------------------------------------------


def test_r8_list_agents_augments_missing_curated_builtins():
    """The eval saw list_agents return 7 while search and the spend ledger
    showed 9 reachable agents (Browser Agent + Visual Regression). The
    fix synthesizes registry-shaped rows from the spec for any curated
    public id not yet in the agents table, so the public surface is
    self-consistent regardless of seed/cache state.
    """
    src = Path("server/application_parts/part_007.py").read_text()
    assert "missing_curated" in src, (
        "registry_list must compute missing_curated from "
        "CURATED_PUBLIC_BUILTIN_AGENT_IDS - present_ids."
    )
    assert "_builtin_specs.builtin_spec_by_id" in src, (
        "registry_list must source missing rows from spec_by_id."
    )


# ---------------------------------------------------------------------------
# R9 — audit verify_all bounded per-receipt
# ---------------------------------------------------------------------------


def test_r9_session_audit_verify_all_is_bounded_server_side():
    """Bulk verify on a long window must never block the audit (eval finding R9).

    Originally enforced client-side: ``_session_audit_legacy`` looped per
    receipt with a 1.0s per-call timeout cap (``_verify_timeout``). The 1.6.x
    refactor moved verification SERVER-SIDE — the SDK now delegates
    ``verify_all`` to ``/wallets/audit``, which Ed25519-verifies in-process
    (sub-50ms each, single HTTP call instead of N) over a window bounded by
    ``limit``. This asserts that bounded, server-delegated design, which
    preserves the original "bulk verify can't block the audit" guarantee.
    """
    # Client side: _session_audit delegates verify_all to the server audit
    # endpoint — no unbounded per-receipt client loop.
    sdk_src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    assert "wallets/audit" in sdk_src and "verify_all" in sdk_src, (
        "_session_audit must delegate verify_all to the server /wallets/audit "
        "endpoint rather than looping per-receipt on the client (eval finding R9)."
    )

    # Server side: the bulk-verify loop runs in-process over the limit-bounded
    # receipt window (``zip(receipts, ...)``), so worst-case work is O(limit),
    # never unbounded.
    server_src = Path("server/application_parts/part_011.py").read_text()
    assert "if verify_all:" in server_src and "zip(receipts" in server_src, (
        "server /wallets/audit must bulk-verify in-process over the "
        "limit-bounded receipt window (eval finding R9)."
    )


# ---------------------------------------------------------------------------
# R10 — feature flag exposure for search floors
# ---------------------------------------------------------------------------


def test_r10_search_floors_are_feature_flagged(monkeypatch):
    """Search relevance/keep/dropoff thresholds must be tunable via env
    so the floor can be adjusted without redeploy. The plan called for
    AZTEA_SEARCH_RELEVANCE_FLOOR, AZTEA_SEARCH_KEEP_FLOOR, AZTEA_SEARCH_DROPOFF_BAND.

    The default for relevance_floor was raised from 0.18 to 0.30 in the
    2026-05-09 rails pass after live calibration: off-catalog queries
    measured 0.23–0.26 in production with the real embedding model and
    current catalog, so 0.18 was below the off-catalog distribution and
    let "tell me a joke" return code-execution agents. 0.30 sits cleanly
    between off-catalog (≤0.26) and legitimate (≥0.33) blended scores.
    """
    from core import feature_flags

    monkeypatch.delenv("AZTEA_SEARCH_RELEVANCE_FLOOR", raising=False)
    monkeypatch.delenv("AZTEA_SEARCH_KEEP_FLOOR", raising=False)
    monkeypatch.delenv("AZTEA_SEARCH_DROPOFF_BAND", raising=False)

    assert callable(feature_flags.search_relevance_floor)
    assert feature_flags.search_relevance_floor() == pytest.approx(0.30, abs=1e-6)
    assert feature_flags.search_keep_floor() == pytest.approx(0.20, abs=1e-6)
    assert feature_flags.search_dropoff_band() == pytest.approx(0.20, abs=1e-6)

    # And the env override actually takes effect.
    monkeypatch.setenv("AZTEA_SEARCH_RELEVANCE_FLOOR", "0.42")
    assert feature_flags.search_relevance_floor() == pytest.approx(0.42, abs=1e-6)

    # And the call site reads them.
    src = Path("core/registry/agents_ops.py").read_text()
    assert "_feature_flags.search_relevance_floor()" in src, (
        "agents_ops.search_agents must read the floor from feature_flags."
    )
