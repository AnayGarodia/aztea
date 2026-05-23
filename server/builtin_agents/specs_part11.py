"""Specs for the 2026-05-22 strategy-doc 25-agent slate.

D16 Codebase Reviewer + C11 Compliance Attestor are the two reference
agents that work today. The other 23 are wired and hireable but return a
structured ``requires_configuration`` envelope until their external infra
(lifecycle runner backend, GitHub App for PR Watch, Stripe API for
Settler, etc.) lands. See ``PENDING_INFRA_AGENT_IDS`` in
``server.builtin_agents.constants`` and the per-agent module docstring
for the exact env vars or files each one needs.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    ADVERSARIAL_RED_TEAMER_AGENT_ID as _ADVERSARIAL_RED_TEAMER_AGENT_ID,
    AI_CODE_PROVENANCE_STAMP_AGENT_ID as _AI_CODE_PROVENANCE_STAMP_AGENT_ID,
    AUTHOR_STYLE_REVIEWER_AGENT_ID as _AUTHOR_STYLE_REVIEWER_AGENT_ID,
    BISECT_AND_BLAME_AGENT_ID as _BISECT_AND_BLAME_AGENT_ID,
    BUILTIN_INTERNAL_ENDPOINTS as _BUILTIN_INTERNAL_ENDPOINTS,
    CODEBASE_REVIEWER_AGENT_ID as _CODEBASE_REVIEWER_AGENT_ID,
    COMPLIANCE_ATTESTOR_AGENT_ID as _COMPLIANCE_ATTESTOR_AGENT_ID,
    DEPLOY_CANARY_PILOT_AGENT_ID as _DEPLOY_CANARY_PILOT_AGENT_ID,
    DMARC_EMAIL_VERIFIER_AGENT_ID as _DMARC_EMAIL_VERIFIER_AGENT_ID,
    FLAKE_HUNTER_AGENT_ID as _FLAKE_HUNTER_AGENT_ID,
    FUZZ_AND_FIND_AGENT_ID as _FUZZ_AND_FIND_AGENT_ID,
    MIGRATION_PILOT_AGENT_ID as _MIGRATION_PILOT_AGENT_ID,
    PR_WATCH_AGENT_ID as _PR_WATCH_AGENT_ID,
    PRIVACY_FLOW_TRACER_AGENT_ID as _PRIVACY_FLOW_TRACER_AGENT_ID,
    PROD_TRACE_REPLAYER_AGENT_ID as _PROD_TRACE_REPLAYER_AGENT_ID,
    PRODUCTION_INCIDENT_CAPTAIN_AGENT_ID as _PRODUCTION_INCIDENT_CAPTAIN_AGENT_ID,
    SCHEMA_MIGRATION_PLANNER_AGENT_ID as _SCHEMA_MIGRATION_PLANNER_AGENT_ID,
    STRIPE_CONNECT_SETTLER_AGENT_ID as _STRIPE_CONNECT_SETTLER_AGENT_ID,
    VULNERABILITY_DISCLOSURE_SUBMITTER_AGENT_ID as _VULNERABILITY_DISCLOSURE_SUBMITTER_AGENT_ID,
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
    CURATED_BUILTIN_AGENT_IDS (D16 + C11 reference agents). The 23
    pending-infra agents bypass the stricter normalization gate.
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
        _spec(
            agent_id=_DEPLOY_CANARY_PILOT_AGENT_ID, name="Deploy Canary Pilot",
            slug="deploy_canary_pilot",
            description=(
                "Ship a canary deploy, watch metrics for the configured window, "
                "and roll back autonomously on SLO breach. v0 requires "
                "AZTEA_DEPLOY_API_TOKEN + AZTEA_METRICS_API_URL."
            ),
            price=15.0, tags=["deploy", "sre", "real-world-action"],
            keywords=["canary", "deploy", "rollback", "slo"],
            input_schema={
                "type": "object",
                "properties": {
                    "deploy_cmd": {"type": "string"},
                    "slo_thresholds": {"type": "object"},
                    "watch_seconds": {"type": "integer", "default": 1800,
                                      "minimum": 60, "maximum": 14400},
                },
                "required": ["deploy_cmd", "slo_thresholds"],
            },
        ),
        _spec(
            agent_id=_MIGRATION_PILOT_AGENT_ID, name="Migration Pilot", slug="migration_pilot",
            description=(
                "Run a DB migration safely on a replica DSN: dry-run, measure lock "
                "contention, pick a zero-downtime strategy, return a runbook. v0 "
                "requires AZTEA_MIGRATION_REPLICA_DSN."
            ),
            price=25.0, tags=["database", "migration", "longitudinal"],
            keywords=["migration", "schema change", "zero downtime"],
            input_schema={
                "type": "object",
                "properties": {
                    "target_sql": {"type": "string"},
                    "lock_threshold_ms": {"type": "integer", "default": 5000},
                    "allow_drops": {"type": "boolean", "default": False},
                },
                "required": ["target_sql"],
            },
        ),
        _spec(
            agent_id=_PR_WATCH_AGENT_ID, name="PR Watch", slug="pr_watch",
            description=(
                "Babysit a GitHub PR for up to 24 hours — re-run flaky CI, retry "
                "infra blips, notify when reviewer stalls. v0 requires the GitHub "
                "App + lifecycle runner backend."
            ),
            price=1.0, tags=["github", "ci", "longitudinal"],
            keywords=["pr watch", "babysit pr", "monitor pr", "re-run ci"],
            input_schema={
                "type": "object",
                "properties": {
                    "pr_url": {"type": "string"},
                    "watch_seconds": {"type": "integer", "default": 14400,
                                      "minimum": 60, "maximum": 86400},
                },
                "required": ["pr_url"],
            },
        ),

        # ----- B family — compute-heavy -----
        _spec(
            agent_id=_FUZZ_AND_FIND_AGENT_ID, name="Fuzz-and-Find", slug="fuzz_and_find",
            description=(
                "Find counterexamples to a stated property by running millions of "
                "Hypothesis-style fuzz trials. v0 requires the lifecycle runner; "
                "the existing quant_patch_validator covers the quant-code special case."
            ),
            price=3.0, tags=["fuzzing", "property-tests", "compute"],
            keywords=["fuzz", "property test", "counterexample"],
            input_schema={
                "type": "object",
                "properties": {
                    "function_source": {"type": "string"},
                    "property_spec": {"type": "string"},
                    "iterations": {"type": "integer", "default": 10000, "maximum": 1000000},
                },
                "required": ["function_source", "property_spec"],
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
            agent_id=_VULNERABILITY_DISCLOSURE_SUBMITTER_AGENT_ID,
            name="Vulnerability Disclosure Submitter",
            slug="vulnerability_disclosure_submitter",
            description=(
                "File a security finding with the vendor through their disclosure "
                "channel. v0 requires the vendor PGP key + an attestable "
                "from-address; refuses to submit otherwise to avoid silent half-sends."
            ),
            price=30.0, tags=["security", "disclosure", "real-world-action"],
            keywords=["cve report", "responsible disclosure", "vendor submit"],
            input_schema={
                "type": "object",
                "properties": {
                    "finding": {"type": "object"},
                    "vendor_disclosure_url": {"type": "string"},
                    "vendor_pgp_pubkey_path": {"type": "string"},
                },
                "required": ["finding", "vendor_disclosure_url"],
            },
        ),
        _spec(
            agent_id=_DMARC_EMAIL_VERIFIER_AGENT_ID, name="DMARC Email Verifier",
            slug="dmarc_email_verifier",
            description=(
                "Pre-flight a real outbound campaign through SMTP, check DMARC / SPF "
                "/ DKIM / blacklists, return per-domain go/no-go. v0 requires "
                "SMTP_HOST/USER/PASS + AZTEA_DMARC_CANARY_INBOX."
            ),
            price=8.0, tags=["email", "deliverability", "real-world-action"],
            keywords=["dmarc verifier", "email send check",
                      "outbound email preflight"],
            input_schema={
                "type": "object",
                "properties": {
                    "sample_email": {"type": "object"},
                    "target_domains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["sample_email", "target_domains"],
            },
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
        _spec(
            agent_id=_PRODUCTION_INCIDENT_CAPTAIN_AGENT_ID, name="Incident Captain",
            slug="production_incident_captain",
            description=(
                "Coordinate the first 30 minutes of a production incident: pull "
                "alerts from PagerDuty/Sentry, correlate with deploys, open a "
                "war-room doc, escalate when confidence > threshold. v0 requires "
                "PAGERDUTY_API_TOKEN + SENTRY_API_TOKEN + AZTEA_INCIDENT_DOC_TARGET."
            ),
            price=15.0, tags=["sre", "incident", "real-world-action"],
            keywords=["incident response", "page", "war room", "oncall"],
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {"type": "string"},
                    "escalation_confidence_threshold": {"type": "number", "default": 0.7},
                },
                "required": ["page_id"],
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
            agent_id=_AUTHOR_STYLE_REVIEWER_AGENT_ID, name="Author-Style Reviewer",
            slug="author_style_reviewer",
            description=(
                "Review a PR as a specific reviewer would, based on their prior "
                "review-comment corpus. v0 requires the corpus path env var; the "
                "underlying ingester ships in v0.1."
            ),
            price=4.0, tags=["code-review", "org-memory"],
            keywords=["author style", "personalised review"],
            input_schema={
                "type": "object",
                "properties": {
                    "repo_id": {"type": "string"},
                    "author_handle": {"type": "string"},
                    "hunks": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["repo_id", "author_handle", "hunks"],
            },
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

        # ----- E family — specialized -----
        _spec(
            agent_id=_ADVERSARIAL_RED_TEAMER_AGENT_ID, name="Adversarial Red-Teamer",
            slug="adversarial_red_teamer",
            description=(
                "Probe an endpoint for exploits against a stated goal. Requires "
                "an explicit consent_token AND the lifecycle runner backend + "
                "AZTEA_REDTEAM_CONSENT_SIGNING_KEY. Refuses without consent."
            ),
            price=80.0, tags=["security", "red-team", "specialized"],
            keywords=["red team", "endpoint attack", "exploit search"],
            input_schema={
                "type": "object",
                "properties": {
                    "target_url": {"type": "string"},
                    "goal": {"type": "string"},
                    "consent_token": {"type": "string"},
                },
                "required": ["target_url", "goal", "consent_token"],
            },
        ),
        _spec(
            agent_id=_PRIVACY_FLOW_TRACER_AGENT_ID, name="Privacy Flow Tracer",
            slug="privacy_flow_tracer",
            description=(
                "Produce a runtime data-flow diagram showing where tagged PII "
                "actually went. v0 requires AZTEA_OTEL_COLLECTOR_URL + "
                "AZTEA_EBPF_AGENT_SOCKET."
            ),
            price=100.0, tags=["privacy", "compliance", "specialized"],
            keywords=["pii trace", "data flow", "gdpr"],
            input_schema={
                "type": "object",
                "properties": {
                    "repo_root": {"type": "string"},
                    "pii_tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["repo_root", "pii_tags"],
            },
        ),
        _spec(
            agent_id=_AI_CODE_PROVENANCE_STAMP_AGENT_ID, name="AI-Code Provenance Stamp",
            slug="ai_code_provenance_stamp",
            description=(
                "Classify each PR hunk as human / AI / mixed and sign the manifest "
                "with the per-server attestation key. The stylometric classifier "
                "is a v0.1 follow-up; v0 uses LLM-based heuristic classification."
            ),
            price=2.0, tags=["provenance", "ai-content", "signed-attestation"],
            keywords=["ai code provenance", "human vs ai code"],
            input_schema={
                "type": "object",
                "properties": {
                    "pr_ref": {"type": "string"},
                    "hunks": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["pr_ref", "hunks"],
            },
        ),
    ]
