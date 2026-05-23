"""Specs for the 2026-05-22 strategy-doc slate (post-editorial cut).

D16 Codebase Reviewer + C11 Compliance Attestor are the two reference
agents that work today. The other five (A1 Flake Hunter, A2 Bisect-and-
Blame, C14 Stripe Connect Settler, D18 Prod Trace Replayer, D19 Schema
Migration Planner) are wired and hireable but return a structured
``requires_configuration`` envelope until their external infra lands.
See ``PENDING_INFRA_AGENT_IDS`` in ``server.builtin_agents.constants`` and
the per-agent module docstring for the exact env vars or files each one
needs.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    BISECT_AND_BLAME_AGENT_ID as _BISECT_AND_BLAME_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    CODEBASE_REVIEWER_AGENT_ID as _CODEBASE_REVIEWER_AGENT_ID,
    COMPLIANCE_ATTESTOR_AGENT_ID as _COMPLIANCE_ATTESTOR_AGENT_ID,
    FLAKE_HUNTER_AGENT_ID as _FLAKE_HUNTER_AGENT_ID,
    PROD_TRACE_REPLAYER_AGENT_ID as _PROD_TRACE_REPLAYER_AGENT_ID,
    SCHEMA_MIGRATION_PLANNER_AGENT_ID as _SCHEMA_MIGRATION_PLANNER_AGENT_ID,
    STRIPE_CONNECT_SETTLER_AGENT_ID as _STRIPE_CONNECT_SETTLER_AGENT_ID,
)
from server.builtin_agents.schemas import output_schema_object as _output_schema_object


def _common_output() -> dict[str, Any]:
    """Output schema shared by every scaffolded agent in this slate."""
    return _output_schema_object(
        {
            "summary": {"type": "string"},
            "plan": {"type": "string"},
            "synthesis": {"type": "string"},
            "trace": {"type": "object"},
            "llm_used": {"type": "boolean"},
        },
        required=[],
    )


def _stub_output_example(slug: str) -> dict[str, Any]:
    """Minimum-shape example: the structured ``requires_configuration``
    envelope this agent returns when its external dep is not yet wired.
    Real ``output_examples`` for fully-working invocations land alongside
    the per-agent infra unlock PRs.
    """
    return {
        "input": {},
        "output": {
            "error": {
                "code": f"{slug}.requires_configuration",
                "message": f"{slug} requires configuration",
                "details": {"missing": ["<see per-agent module docstring>"]},
            }
        },
    }


def _spec(
    *, agent_id: str, name: str, slug: str, description: str,
    price: float, tags: list[str], keywords: list[str],
    input_schema: dict[str, Any], output_schema: dict[str, Any] | None = None,
    output_examples: list[dict[str, Any]] | None = None,
    category: str | None = None,
    cacheable: bool = False,
) -> dict[str, Any]:
    """Tight spec builder so each agent below stays ~10 lines.

    ``category`` + ``cacheable`` are only required for agents in
    CURATED_BUILTIN_AGENT_IDS (D16 + C11 reference agents). The pending-
    infra agents bypass the stricter normalization gate.
    """
    spec: dict[str, Any] = {
        "agent_id": agent_id,
        "name": name,
        "description": description,
        "endpoint_url": _BUILTIN_INTERNAL_ENDPOINTS[agent_id],
        "price_per_call_usd": price,
        "tags": tags,
        "match_keywords": keywords,
        "input_schema": input_schema,
        "output_schema": output_schema or _common_output(),
        "output_examples": output_examples or [_stub_output_example(slug)],
    }
    if category is not None:
        spec["category"] = category
        spec["cacheable"] = cacheable
    return spec


def load_builtin_specs_part11() -> list[dict[str, Any]]:
    return [
        # ----- A family — longitudinal -----
        _spec(
            agent_id=_FLAKE_HUNTER_AGENT_ID, name="Flake Hunter", slug="flake_hunter",
            description=(
                "Characterise and (where possible) fix a flaky test by running it "
                "1000x in parallel across seeds, env vars, and parallelism. v0 "
                "returns requires_configuration until AZTEA_RUNNER_JOB_LIFECYCLE_ENABLED=1."
            ),
            price=2.0, tags=["testing", "ci", "longitudinal"],
            keywords=["flaky test", "intermittent test", "test reruns"],
            input_schema={
                "type": "object",
                "properties": {
                    "test_path": {"type": "string"},
                    "repo_root": {"type": "string", "description": "absolute path"},
                    "trials": {"type": "integer", "default": 200, "minimum": 1, "maximum": 1000},
                },
                "required": ["test_path", "repo_root"],
            },
        ),
        _spec(
            agent_id=_BISECT_AND_BLAME_AGENT_ID, name="Bisect-and-Blame", slug="bisect_and_blame",
            description=(
                "Localise a regression to a specific commit via parallel git bisect "
                "with a caller-supplied benchmark predicate. v0 returns "
                "requires_configuration without the lifecycle runner backend."
            ),
            price=1.5, tags=["debugging", "git", "longitudinal"],
            keywords=["bisect", "regression", "performance regressed", "find the commit"],
            input_schema={
                "type": "object",
                "properties": {
                    "good_ref": {"type": "string"},
                    "bad_ref": {"type": "string"},
                    "repro_cmd": {"type": "string"},
                },
                "required": ["good_ref", "bad_ref", "repro_cmd"],
            },
        ),

        # ----- C family — liability-bearing -----
        _spec(
            category="Compliance",
            cacheable=False,
            agent_id=_COMPLIANCE_ATTESTOR_AGENT_ID, name="Compliance Attestor",
            slug="compliance_attestor",
            description=(
                "Signs an Ed25519 attestation that a PR satisfies a named control "
                "(SOC2_CC6_1 in v0). Refuses to sign if any required check is "
                "missing or failed; returns a per-check evidence ledger either way."
            ),
            price=20.0, tags=["compliance", "soc2", "signed-attestation"],
            keywords=["compliance attestation", "soc2", "hipaa", "audit evidence"],
            input_schema={
                "type": "object",
                "properties": {
                    "control": {"type": "string", "enum": ["SOC2_CC6_1"]},
                    "pr_ref": {"type": "string"},
                    "check_results": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["control", "pr_ref", "check_results"],
            },
            output_schema=_output_schema_object(
                {
                    "control": {"type": "string"},
                    "pr_ref": {"type": "string"},
                    "status": {"type": "string"},
                    "attestation": {"type": "object"},
                    "signature_b64": {"type": "string"},
                    "trace": {"type": "object"},
                },
                required=["control", "status"],
            ),
        ),
        _spec(
            agent_id=_STRIPE_CONNECT_SETTLER_AGENT_ID, name="Stripe Connect Settler",
            slug="stripe_connect_settler",
            description=(
                "Monthly signed Stripe-vs-ledger reconciliation. v0 requires "
                "STRIPE_API_KEY and a caller-supplied internal_ledger_source path."
            ),
            price=80.0, tags=["payments", "reconciliation", "signed-attestation"],
            keywords=["stripe reconciliation", "ledger drift", "monthly statement"],
            input_schema={
                "type": "object",
                "properties": {
                    "month": {"type": "string", "pattern": "^\\d{4}-\\d{2}$"},
                    "internal_ledger_source": {"type": "string"},
                },
                "required": ["month", "internal_ledger_source"],
            },
        ),

        # ----- D family — org-memory -----
        _spec(
            category="Code Quality",
            cacheable=False,
            agent_id=_CODEBASE_REVIEWER_AGENT_ID, name="Codebase Reviewer (yours)",
            slug="codebase_reviewer",
            description=(
                "Review a PR through the lens of your own repo's bug history. For "
                "each candidate hunk: retrieve top-K similar past changes, check "
                "whether each was reverted / linked to an incident, and produce a "
                "calibrated review citing specific past commits. Requires the repo "
                "to be ingested first via core.hosted_index.ingest_repo."
            ),
            price=8.0, tags=["code-review", "org-memory", "developer-tools"],
            keywords=["code review", "pr review", "regression risk"],
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string"},
                    "hunks": {"type": "array", "items": {"type": "object"}},
                    "max_hunks": {"type": "integer", "default": 10, "maximum": 25},
                    "k_per_hunk": {"type": "integer", "default": 5, "maximum": 10},
                    "budget_cents": {"type": "integer", "default": 50},
                },
                "required": ["repo_id", "hunks"],
            },
            output_schema=_output_schema_object(
                {
                    "summary": {"type": "string"},
                    "confidence": {"type": "string"},
                    "findings": {"type": "array"},
                    "trace": {"type": "object"},
                },
                required=["summary", "findings"],
            ),
        ),
        _spec(
            agent_id=_PROD_TRACE_REPLAYER_AGENT_ID, name="Prod Trace Replayer",
            slug="prod_trace_replayer",
            description=(
                "Replay sanitized prod traces against a candidate build and report "
                "behavior diffs. v0 requires the lifecycle runner backend + a "
                "trace bundle on disk."
            ),
            price=10.0, tags=["testing", "regression", "org-memory"],
            keywords=["prod traffic replay", "shadow test", "behaviour diff"],
            input_schema={
                "type": "object",
                "properties": {
                    "candidate_url": {"type": "string"},
                    "trace_bundle_path": {"type": "string"},
                },
                "required": ["candidate_url", "trace_bundle_path"],
            },
        ),
        _spec(
            agent_id=_SCHEMA_MIGRATION_PLANNER_AGENT_ID, name="Schema Migration Planner",
            slug="schema_migration_planner",
            description=(
                "Produce a zero-downtime migration plan verified against your real "
                "production query log. v0 requires the query log path."
            ),
            price=40.0, tags=["database", "migration", "org-memory"],
            keywords=["schema migration plan", "zero downtime migration"],
            input_schema={
                "type": "object",
                "properties": {
                    "current_schema": {"type": "string"},
                    "target_schema": {"type": "string"},
                    "query_log_path": {"type": "string"},
                },
                "required": ["current_schema", "target_schema", "query_log_path"],
            },
        ),
    ]
