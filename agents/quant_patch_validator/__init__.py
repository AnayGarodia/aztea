"""quant_patch_validator — differential fuzzer for AI-written quant code.

# OWNS: the `run(payload)` entry point invoked by the built-in agent
#        dispatcher; orchestrates signature inference, harness build,
#        fuzz, cluster, triage, and report assembly.
# NOT OWNS: the implementation of each stage (lives in sibling modules).
# INVARIANTS:
#   - `run` ALWAYS returns a dict. It NEVER raises out of the agent.
#     Internal failures are converted to structured `{"error": ...}`
#     envelopes; ladder degradations (no LLM) take the heuristic path.
#   - The output schema documented in the spec entry is a hard
#     contract — adding a field is fine, removing or renaming one is
#     not.
# DECISIONS:
#   - Tier → budget_seconds mapping is the SINGLE source of truth.
#     The spec entry references this constant via a string lookup;
#     don't duplicate the numbers elsewhere.
#   - Workspace artifact writes are best-effort: failure to write the
#     audit log does not fail the call. This mirrors how
#     core/pipelines/executor.py handles workspace I/O.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents._contracts import agent_error
from agents.quant_patch_validator import (
    cluster as _cluster_mod,
    coverage_track as _coverage_mod,
    fuzz as _fuzz_mod,
    fuzz_atheris as _atheris_mod,
    harness as _harness_mod,
    report as _report_mod,
    signature as _signature_mod,
    triage as _triage_mod,
)

_LOG = logging.getLogger("aztea.agents.quant_patch_validator")

# Tier → budget seconds. Single source of truth; spec entry references
# these via the `pricing_config.tiers` mapping but the numbers below
# determine the actual wall-clock budget.
_TIER_BUDGETS: dict[str, int] = {
    "quick": 30,
    "standard": 300,
    "deep": 1800,
}
_DEFAULT_TIER = "standard"
_VALID_ENGINES = {"hypothesis", "atheris"}
_MAX_SOURCE_BYTES = 64_000  # caller-side hard cap on either source string

# Imports that would recursively re-enter the validator. We block the
# plain `import agents.quant_patch_validator` / `from
# agents.quant_patch_validator import ...` patterns via AST inspection.
# A determined attacker can still bypass with `__import__("agents." +
# "quant_patch_validator")` — true containment requires `live_sandbox`,
# which is an explicit v0.2 deferral. See
# `docs/runbooks/quant-patch-validator.md` for the threat model.
_SELF_MODULE = "agents.quant_patch_validator"


def _contains_self_import(source: str) -> bool:
    """Return True if `source` statically imports this package.

    Uses `ast` so we tolerate whitespace / aliasing variants. Returns
    False on a syntax error — that case is caught by signature parsing
    downstream and surfaces a different error envelope.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _SELF_MODULE or alias.name.startswith(_SELF_MODULE + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == _SELF_MODULE or mod.startswith(_SELF_MODULE + "."):
                return True
    return False


def _validate_payload(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return agent_error(
            "quant_patch_validator.invalid_payload",
            f"payload must be dict, got {type(payload).__name__}",
        )
    if not isinstance(payload.get("reference_code"), str) or not payload["reference_code"].strip():
        return agent_error(
            "quant_patch_validator.missing_reference_code",
            "reference_code is required and must be a non-empty string",
        )
    if not isinstance(payload.get("candidate_code"), str) or not payload["candidate_code"].strip():
        return agent_error(
            "quant_patch_validator.missing_candidate_code",
            "candidate_code is required and must be a non-empty string",
        )
    if len(payload["reference_code"].encode()) > _MAX_SOURCE_BYTES:
        return agent_error(
            "quant_patch_validator.reference_too_large",
            f"reference_code exceeds {_MAX_SOURCE_BYTES}-byte limit",
        )
    if len(payload["candidate_code"].encode()) > _MAX_SOURCE_BYTES:
        return agent_error(
            "quant_patch_validator.candidate_too_large",
            f"candidate_code exceeds {_MAX_SOURCE_BYTES}-byte limit",
        )
    tier = payload.get("fuzz_budget", _DEFAULT_TIER)
    if tier not in _TIER_BUDGETS:
        return agent_error(
            "quant_patch_validator.invalid_fuzz_budget",
            f"fuzz_budget must be one of {sorted(_TIER_BUDGETS)}, got {tier!r}",
        )
    engine = payload.get("fuzz_engine", "hypothesis")
    if engine not in _VALID_ENGINES:
        return agent_error(
            "quant_patch_validator.invalid_fuzz_engine",
            f"fuzz_engine must be one of {sorted(_VALID_ENGINES)}, got {engine!r}",
        )
    return None


def _run_fuzz_engine(
    engine: str,
    h,
    enrichment: dict,
    budget: float,
    rtol: float,
    atol: float,
    auto_tune: bool,
):
    """Dispatch to the chosen fuzz engine; atheris falls back to hypothesis."""
    if engine == "atheris" and _atheris_mod.is_available():
        return _atheris_mod.run_atheris_fuzz(
            h, enrichment, budget_seconds=budget, rtol=rtol, atol=atol
        )
    return _fuzz_mod.run_fuzz(
        h,
        enrichment,
        budget_seconds=budget,
        rtol=rtol,
        atol=atol,
        auto_tune=auto_tune,
    )


def _write_workspace_artifact(workspace_id: str, path: str, body: dict | str) -> None:
    """Best-effort write of an audit artifact. Never raises."""
    try:
        from core import workspaces as _workspaces

        encoded = json.dumps(body, indent=2, default=str).encode("utf-8") if isinstance(body, dict) else str(body).encode("utf-8")
        _workspaces.write_artifact(
            workspace_id,
            path,
            encoded,
            "application/json" if isinstance(body, dict) else "text/plain",
            created_by_agent_id="quant_patch_validator",
            created_by_job_id=None,
        )
    except Exception as exc:  # noqa: BLE001
        # Documented best-effort. Audit trail loss should not fail validation.
        _LOG.warning("workspace artifact write failed: %s", exc)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate an AI-written patch against a reference implementation.

    Returns a structured dict; never raises.
    """
    validation_error = _validate_payload(payload)
    if validation_error is not None:
        return validation_error

    reference = payload["reference_code"]
    candidate = payload["candidate_code"]

    # Defence-in-depth: reject any candidate / reference that recursively
    # imports this validator. See `_contains_self_import` for the threat
    # model + escape-hatch documentation.
    for side, src in (("reference", reference), ("candidate", candidate)):
        if _contains_self_import(src):
            return agent_error(
                "quant_patch_validator.self_reference_blocked",
                f"{side}_code imports {_SELF_MODULE}; recursive invocation is rejected",
                {"side": side, "module": _SELF_MODULE},
            )
    tier = payload.get("fuzz_budget", _DEFAULT_TIER)
    # Optional override: caller can pin an exact wall-clock budget. Used by
    # low-latency sync callers (the sync gateway has an 8s cap; the deep tier
    # would always hit it). Capped at the tier's nominal budget.
    fuzz_seconds_override = payload.get("fuzz_seconds")
    engine = payload.get("fuzz_engine", "hypothesis")
    rtol = float(payload.get("rtol", 1e-5))
    atol = float(payload.get("atol", 1e-7))
    # Auto-tune is OFF by default: permuting the reference's input to
    # measure self-divergence works for stateless functions (mean, var)
    # but produces catastrophic over-tolerance for time-ordered functions
    # (RSI, rolling stats) where the "permuted" output is supposed to be
    # very different. Default rtol/atol above are calibrated empirically
    # for the typical AI failure range. Callers can opt in to auto-tune
    # for known-stateless functions.
    auto_tune = bool(payload.get("auto_tune_tolerance", False))
    spec_hint = payload.get("spec_hint")
    workspace_id = payload.get("_workspace_id")
    caller_api_key_id = payload.get("_caller_api_key_id")

    # ---- Stage 1: signature inference ----
    pair = _signature_mod.infer_pair(reference, candidate)
    if pair is None:
        return agent_error(
            "quant_patch_validator.signature_parse_failed",
            "Could not extract a callable function from one of the two sources. "
            "Both must define at least one top-level def with a unique name.",
        )
    if pair.divergence is not None:
        # Hard stop: signatures don't match. We cannot fuzz.
        report = _report_mod.build_report(
            signature_pair=pair,
            fuzz=None,
            clusters=[],
            triaged=[],
            tier_used=tier,
            spec_hint=spec_hint,
        )
        report["verdict"] = "signature_divergence"
        if workspace_id:
            _write_workspace_artifact(workspace_id, "qpv/signature_divergence.json", report)
        return report

    # Optional LLM enrichment of generation constraints
    enrichment = _signature_mod.llm_enrich_constraints(
        pair.reference, spec_hint=spec_hint, caller_api_key_id=caller_api_key_id
    )

    # ---- Stage 2: build the harness ----
    track_coverage = bool(payload.get("track_coverage", False))
    budget = _TIER_BUDGETS[tier]
    if fuzz_seconds_override is not None:
        try:
            override = float(fuzz_seconds_override)
            if 1.0 <= override <= budget:
                budget = override
        except (TypeError, ValueError):
            pass  # invalid override → silently use tier default

    coverage_pct: float | None = None

    # Coverage tracking is a v1 limitation under candidate isolation:
    # the candidate runs in a subprocess (see isolation.py) so the
    # in-process `sys.settrace` instrumentation from coverage.py does
    # not observe its execution. We accept the `track_coverage`
    # parameter for forward compatibility but log a warning and skip
    # the coverage context when isolation is active. v0.2 plan: invoke
    # coverage.py inside the worker and pipe the data file back.
    if track_coverage:
        _LOG.info(
            "quant_patch_validator: track_coverage=true requested but candidate "
            "runs in isolated subprocess (v1); coverage_pct will be null. "
            "v0.2 will pipe coverage data from inside the worker."
        )
    try:
        h = _harness_mod.Harness(reference, candidate, pair.reference)
    except Exception as exc:  # noqa: BLE001
        return agent_error(
            "quant_patch_validator.harness_build_failed",
            f"Could not build harness: {type(exc).__name__}: {exc}",
        )
    try:
        fuzz = _run_fuzz_engine(engine, h, enrichment, budget, rtol, atol, auto_tune)
    finally:
        h.close()

    # ---- Stage 4: cluster ----
    clusters = _cluster_mod.cluster_divergences(fuzz.divergences)

    # ---- Stage 5: triage ----
    triaged = _triage_mod.triage_clusters(
        clusters, spec_hint=spec_hint, caller_api_key_id=caller_api_key_id
    )

    # ---- Stage 6: report ----
    report = _report_mod.build_report(
        signature_pair=pair,
        fuzz=fuzz,
        clusters=clusters,
        triaged=triaged,
        tier_used=tier,
        spec_hint=spec_hint,
    )
    # Wire the optional coverage value into the stats block.
    if coverage_pct is not None and "fuzz_stats" in report:
        report["fuzz_stats"]["coverage_pct"] = coverage_pct

    if workspace_id:
        _write_workspace_artifact(workspace_id, "qpv/report.json", report)

    return report
