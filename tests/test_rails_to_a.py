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


def test_r2_python_executor_skips_explainer_on_timeout():
    """The 2026-05-08 eval saw `while True: pass` time out at exit 124 and
    THEN spend ~300ms running an LLM 'explanation' that added zero insight.
    The conditional that gates the explainer must include `not timed_out`.
    """
    src = Path("agents/python_executor.py").read_text()
    # Find the explainer-gating conditional. We assert it short-circuits
    # on `timed_out` rather than the older form that only checked exit_code.
    assert "if explain and not timed_out" in src, (
        "Explainer must skip when timed_out is True (eval finding R2)."
    )


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
    src = Path("scripts/aztea_mcp_server.py").read_text()
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
    src = Path("scripts/aztea_mcp_server.py").read_text()
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
    assert reason.code == "dispute.window_expired"


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


def test_r9_session_audit_verify_all_caps_per_receipt_timeout():
    """Bulk verify on a long window must never block the audit. The fix
    caps each verify call's timeout at 1.0s so the worst case for N
    receipts is N × 1s + network overhead.
    """
    src = Path("scripts/aztea_mcp_meta_tools.py").read_text()
    assert "_verify_timeout" in src and "min(float(timeout or 1.0), 1.0)" in src, (
        "verify_all loop must cap per-call timeout (eval finding R9)."
    )


# ---------------------------------------------------------------------------
# R10 — feature flag exposure for search floors
# ---------------------------------------------------------------------------


def test_r10_search_floors_are_feature_flagged():
    """Search relevance/keep/dropoff thresholds must be tunable via env
    so the floor can be adjusted without redeploy. The plan called for
    AZTEA_SEARCH_RELEVANCE_FLOOR, AZTEA_SEARCH_KEEP_FLOOR, AZTEA_SEARCH_DROPOFF_BAND.
    """
    from core import feature_flags

    # Defaults match the legacy literals.
    assert callable(feature_flags.search_relevance_floor)
    assert feature_flags.search_relevance_floor() == pytest.approx(0.18, abs=1e-6)
    assert feature_flags.search_keep_floor() == pytest.approx(0.20, abs=1e-6)
    assert feature_flags.search_dropoff_band() == pytest.approx(0.20, abs=1e-6)

    # And the call site reads them.
    src = Path("core/registry/agents_ops.py").read_text()
    assert "_feature_flags.search_relevance_floor()" in src, (
        "agents_ops.search_agents must read the floor from feature_flags."
    )
