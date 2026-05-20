"""Triage a `DivergenceCluster` into expected / regression / both_wrong.

# OWNS: deciding for each cluster whether the divergence is:
#         - EXPECTED   — intended by the spec / spec_hint (e.g. the patch
#                         is a documented bug fix)
#         - REGRESSION — unintended; the candidate behaviour is wrong
#         - BOTH_WRONG — both ref and cand diverge from what the
#                         spec says they should do
#       and producing a human-readable hypothesis for each.
# NOT OWNS: fuzzing / clustering / oracle.
# INVARIANTS:
#   - When LLM is unavailable (no key configured, all providers fail,
#     bad JSON response), we MUST still produce a verdict per cluster.
#     Heuristic fallback: every divergence becomes REGRESSION except
#     when both sides raise (BOTH_WRONG). That's the conservative
#     default — quant teams prefer "we flagged it, you decide" over
#     "we silently dropped it."
#   - The LLM is allowed to mark a cluster EXPECTED only when
#     `spec_hint` is provided. Without a spec_hint, EXPECTED is
#     never the right answer; an "intended fix" requires a stated
#     intent.
# DECISIONS:
#   - One LLM call per cluster, not one per divergence. Clusters are
#     typically ≤10, so the prompt budget stays bounded.
#   - We never let the LLM downgrade an `exception_mismatch` divergence
#     to EXPECTED. That class of divergence is structural — if the
#     candidate raises and the reference doesn't, the candidate is
#     wrong, full stop.
# KNOWN DEBT:
#   - We don't yet show the LLM the actual reference vs candidate
#     source code; only the divergence record. Including a unified
#     diff between the two would improve triage quality but blows up
#     the prompt budget. Stretch v0.2.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from agents.quant_patch_validator.cluster import DivergenceCluster

# Verdict vocabulary — these strings appear in the public agent output.
VERDICT_REGRESSION = "regression"
VERDICT_EXPECTED = "expected"
VERDICT_BOTH_WRONG = "both_wrong"

_ALL_VERDICTS = {VERDICT_REGRESSION, VERDICT_EXPECTED, VERDICT_BOTH_WRONG}


@dataclass(frozen=True)
class TriagedCluster:
    cluster_id: str
    verdict: str  # one of _ALL_VERDICTS
    hypothesis: str
    confidence: float  # 0..1
    triaged_by: str  # 'llm' | 'heuristic'


_TRIAGE_SYSTEM = (
    "You are a quant-code patch triage assistant. Given a divergence "
    "between a REFERENCE and a CANDIDATE implementation, classify the "
    "divergence as one of:\n"
    " - regression: the candidate behaviour is wrong\n"
    " - expected:  the divergence is the INTENDED EFFECT of the patch, "
    "consistent with the spec_hint provided\n"
    " - both_wrong: both sides are wrong relative to the stated spec\n"
    "Return ONLY a single-line JSON object: "
    '{"verdict": str, "hypothesis": str, "confidence": float}\n'
    "Be conservative: mark 'expected' ONLY when the spec_hint clearly "
    "describes the divergence as intended. If unsure, return 'regression'."
)


# ---------------------------------------------------------------------------
# Heuristic fallback (always works, no network)
# ---------------------------------------------------------------------------


def _heuristic_triage(cluster: DivergenceCluster) -> TriagedCluster:
    rep = cluster.representative
    if rep.divergence_kind == "exception_mismatch":
        if rep.ref.raised and rep.cand.raised:
            return TriagedCluster(
                cluster_id=cluster.cluster_id,
                verdict=VERDICT_BOTH_WRONG,
                hypothesis=(
                    f"Both reference and candidate raise on this input. "
                    f"ref={rep.ref.exception_type}, cand={rep.cand.exception_type}. "
                    "Likely an edge case neither implementation handles."
                ),
                confidence=0.6,
                triaged_by="heuristic",
            )
        if rep.cand.raised and not rep.ref.raised:
            return TriagedCluster(
                cluster_id=cluster.cluster_id,
                verdict=VERDICT_REGRESSION,
                hypothesis=(
                    f"Candidate raises {rep.cand.exception_type} where reference returns cleanly. "
                    "Robustness regression."
                ),
                confidence=0.85,
                triaged_by="heuristic",
            )
        # ref raised, cand did not
        return TriagedCluster(
            cluster_id=cluster.cluster_id,
            verdict=VERDICT_REGRESSION,
            hypothesis=(
                f"Reference raises {rep.ref.exception_type}, candidate silently returns a value. "
                "Likely the candidate is missing a validation check."
            ),
            confidence=0.75,
            triaged_by="heuristic",
        )
    if rep.divergence_kind == "shape":
        return TriagedCluster(
            cluster_id=cluster.cluster_id,
            verdict=VERDICT_REGRESSION,
            hypothesis="Output shape / type differs from reference — contract change.",
            confidence=0.95,
            triaged_by="heuristic",
        )
    # value drift
    det = rep.divergence_detail or {}
    max_diff = det.get("max_abs_diff") or det.get("abs_diff") or 0.0
    return TriagedCluster(
        cluster_id=cluster.cluster_id,
        verdict=VERDICT_REGRESSION,
        hypothesis=(
            f"Numerical divergence (max |Δ| ≈ {float(max_diff):.3g}). "
            f"Without a spec_hint declaring this as the intended change, "
            "treating as unintended regression."
        ),
        confidence=0.7,
        triaged_by="heuristic",
    )


# ---------------------------------------------------------------------------
# LLM-backed triage (preferred when available)
# ---------------------------------------------------------------------------


def _llm_triage_one(
    cluster: DivergenceCluster,
    spec_hint: str | None,
    caller_api_key_id: str | None,
) -> TriagedCluster | None:
    try:
        from core.llm import CompletionRequest, Message, run_with_fallback
        from core.llm.errors import LLMError
    except ImportError:
        return None

    rep = cluster.representative
    payload = {
        "cluster_id": cluster.cluster_id,
        "divergence_kind": rep.divergence_kind,
        "member_count": cluster.member_count,
        "ref_outcome": (
            "raised:" + (rep.ref.exception_type or "")
            if rep.ref.raised
            else "value"
        ),
        "cand_outcome": (
            "raised:" + (rep.cand.exception_type or "")
            if rep.cand.raised
            else "value"
        ),
        "detail": rep.divergence_detail or {},
        "inputs_repr": rep.inputs_repr,
        "spec_hint": (spec_hint or "").strip()[:1500],
    }
    user = json.dumps(payload, default=str)[:4000]
    req = CompletionRequest(
        model="",
        messages=[Message(role="system", content=_TRIAGE_SYSTEM), Message(role="user", content=user)],
        temperature=0.0,
        max_tokens=300,
    )
    try:
        raw = run_with_fallback(req, caller_api_key_id=caller_api_key_id)
        text = (raw.text or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
    except (LLMError, json.JSONDecodeError, Exception):  # noqa: BLE001
        return None

    verdict = str(parsed.get("verdict") or "").lower().strip()
    if verdict not in _ALL_VERDICTS:
        return None
    # Structural divergences cannot be 'expected' — that's an invariant.
    if rep.divergence_kind in ("exception_mismatch", "shape") and verdict == VERDICT_EXPECTED:
        verdict = VERDICT_REGRESSION
    # 'expected' without spec_hint is also disallowed.
    if verdict == VERDICT_EXPECTED and not (spec_hint or "").strip():
        verdict = VERDICT_REGRESSION
    return TriagedCluster(
        cluster_id=cluster.cluster_id,
        verdict=verdict,
        hypothesis=str(parsed.get("hypothesis") or "")[:600],
        confidence=float(parsed.get("confidence") or 0.5),
        triaged_by="llm",
    )


def triage_clusters(
    clusters: list[DivergenceCluster],
    *,
    spec_hint: str | None = None,
    caller_api_key_id: str | None = None,
) -> list[TriagedCluster]:
    """For each cluster, call the LLM; fall back to heuristic on failure."""
    out: list[TriagedCluster] = []
    for cluster in clusters:
        result = _llm_triage_one(cluster, spec_hint, caller_api_key_id)
        if result is None:
            result = _heuristic_triage(cluster)
        out.append(result)
    return out
