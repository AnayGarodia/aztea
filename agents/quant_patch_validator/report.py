"""Assemble the agent's structured public output.

# OWNS: turning a `FuzzResult`, `DivergenceCluster[]`, and `TriagedCluster[]`
#        into the documented output schema that the renderer in
#        core/output_formats.py can decorate.
# NOT OWNS: anything about ingestion, fuzzing, clustering, or triage.
# INVARIANTS:
#   - Every field in the output schema is JSON-serialisable. No
#     dataclass instances, no numpy scalars, no enum members.
#   - The output always includes a `fuzz_stats` block — even when no
#     divergences were found — so callers can decide whether their
#     budget was sufficient.
#   - `confirmed_regressions` and `expected_divergences` are MUTUALLY
#     EXCLUSIVE lists. A cluster appears in exactly one of them based
#     on its triage verdict.
# DECISIONS:
#   - `verdict_summary` is computed from the contents — not the LLM —
#     so it's deterministic. If any cluster is `regression` or
#     `both_wrong`, the overall verdict is `regressions_found`. Else
#     if any are `expected`, it's `intended_changes_only`. Else
#     `equivalent`.
"""

from __future__ import annotations

from typing import Any

from agents.quant_patch_validator.cluster import DivergenceCluster
from agents.quant_patch_validator.fuzz import FuzzResult
from agents.quant_patch_validator.harness import DiffRecord
from agents.quant_patch_validator.signature import SignaturePair
from agents.quant_patch_validator.triage import (
    TriagedCluster,
    VERDICT_BOTH_WRONG,
    VERDICT_EXPECTED,
    VERDICT_REGRESSION,
)


def _diff_to_json(rep: DiffRecord) -> dict[str, Any]:
    return {
        "inputs": rep.inputs_repr,
        "ref": {
            "raised": rep.ref.raised,
            "exception_type": rep.ref.exception_type,
            "exception_msg": rep.ref.exception_msg,
        },
        "cand": {
            "raised": rep.cand.raised,
            "exception_type": rep.cand.exception_type,
            "exception_msg": rep.cand.exception_msg,
        },
        "divergence_kind": rep.divergence_kind,
        "divergence_detail": rep.divergence_detail,
    }


def _cluster_to_json(c: DivergenceCluster, t: TriagedCluster) -> dict[str, Any]:
    return {
        "cluster_id": c.cluster_id,
        "divergence_kind": c.divergence_kind,
        "member_count": c.member_count,
        "verdict": t.verdict,
        "hypothesis": t.hypothesis,
        "confidence": round(float(t.confidence), 3),
        "triaged_by": t.triaged_by,
        "representative": _diff_to_json(c.representative),
    }


def _is_contract_break(cluster: DivergenceCluster) -> bool:
    """A cluster represents a contract break iff the candidate's output
    type / shape / exception behaviour is fundamentally different from
    the reference. These cases would break every caller of the function,
    regardless of input — distinguishing them from value-drift bugs is
    important for routing the right severity to the user.
    """
    rep = cluster.representative
    if rep.divergence_kind == "shape":
        det = rep.divergence_detail or {}
        # Top-level type mismatch (e.g. returns dict vs ndarray) — contract break.
        if det.get("ref_type") and det.get("cand_type") and det["ref_type"] != det["cand_type"]:
            return True
    if rep.divergence_kind == "exception_mismatch":
        # Candidate ALWAYS raises while reference never does — contract break.
        if rep.cand.raised and not rep.ref.raised and cluster.member_count >= 5:
            return True
    return False


def _verdict_summary(
    triaged: list[TriagedCluster],
    clusters: list[DivergenceCluster],
) -> str:
    cluster_by_id = {c.cluster_id: c for c in clusters}
    if any(_is_contract_break(cluster_by_id[t.cluster_id]) for t in triaged if t.cluster_id in cluster_by_id):
        return "contract_broken"
    if any(t.verdict in (VERDICT_REGRESSION, VERDICT_BOTH_WRONG) for t in triaged):
        return "regressions_found"
    if any(t.verdict == VERDICT_EXPECTED for t in triaged):
        return "intended_changes_only"
    return "equivalent"


def build_report(
    *,
    signature_pair: SignaturePair | None,
    fuzz: FuzzResult | None,
    clusters: list[DivergenceCluster],
    triaged: list[TriagedCluster],
    tier_used: str,
    spec_hint: str | None,
) -> dict[str, Any]:
    """Assemble the canonical output dict."""
    by_id = {t.cluster_id: t for t in triaged}
    cluster_rows = [_cluster_to_json(c, by_id[c.cluster_id]) for c in clusters]

    confirmed_regressions = [
        row for row in cluster_rows if row["verdict"] in (VERDICT_REGRESSION, VERDICT_BOTH_WRONG)
    ]
    expected_divergences = [row for row in cluster_rows if row["verdict"] == VERDICT_EXPECTED]
    overall = _verdict_summary(triaged, clusters)

    signature_divergence: dict[str, Any] | None = None
    sig_block: dict[str, Any] | None = None
    if signature_pair is not None:
        sig_block = {
            "function_name": signature_pair.reference.function_name,
            "positional_arity": signature_pair.reference.positional_arity,
            "parameter_types": [
                {"name": p.name, "type": p.type_name, "has_default": p.has_default, "kw_only": p.kw_only}
                for p in signature_pair.reference.parameters
            ],
        }
        if signature_pair.divergence is not None:
            signature_divergence = signature_pair.divergence

    stats: dict[str, Any] = {
        "tier_used": tier_used,
        "fuzz_seconds": (fuzz.elapsed_s if fuzz else 0.0),
        "inputs_explored": (fuzz.inputs_explored if fuzz else 0),
        "divergences_found": (len(fuzz.divergences) if fuzz else 0),
        "clusters": len(clusters),
        "rtol_used": (fuzz.rtol_used if fuzz else None),
        "atol_used": (fuzz.atol_used if fuzz else None),
        "auto_tuned": (fuzz.auto_tuned if fuzz else False),
        "coverage_pct": None,  # populated by future coverage.py integration
    }

    return {
        "verdict": overall,
        "signature": sig_block,
        "signature_divergence": signature_divergence,
        "confirmed_regressions": confirmed_regressions,
        "expected_divergences": expected_divergences,
        "fuzz_stats": stats,
        "spec_hint_used": bool((spec_hint or "").strip()),
    }
