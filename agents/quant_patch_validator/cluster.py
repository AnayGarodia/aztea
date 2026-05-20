"""Cluster divergences and pick a minimal representative per cluster.

# OWNS: grouping `DiffRecord[]` by similarity signature and choosing one
#        representative per group (the smallest, simplest one).
# NOT OWNS: the diff oracle (harness.py), triage / classification
#            (triage.py).
# INVARIANTS:
#   - Clustering is deterministic. Same input list → same output
#     ordering, same representative selection.
#   - We NEVER discard divergences. Every input the fuzzer flagged is
#     either picked as a cluster representative or recorded in its
#     cluster's `member_count`. That lets triage see "this cluster has
#     1247 supporting examples" vs "this cluster has 1 — likely noise."
# DECISIONS:
#   - The cluster signature is `(divergence_kind, exception_pair,
#     shape_pair, magnitude_bucket)` — coarse enough that genuinely
#     related failures (e.g. all NaN-pattern mismatches) collapse to
#     one row, fine enough that distinct bugs stay separate.
#   - "Smallest" representative = shortest inputs_repr string. That
#     correlates well with smallest array length, fewest parameters,
#     etc. Real shrinking (binary-search input minimisation) is a
#     stretch feature for v0.2.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from agents.quant_patch_validator.harness import DiffRecord


@dataclass(frozen=True)
class DivergenceCluster:
    """A coarse equivalence class of divergences."""

    cluster_id: str
    divergence_kind: str
    member_count: int
    representative: DiffRecord
    signature: tuple


def _magnitude_bucket(detail: dict | None) -> str:
    """Bucket the divergence magnitude on a log scale.

    A buy/sell sign flip, an off-by-one in a moving average, and a 100x
    unit-confusion bug have wildly different magnitudes; we want them
    in different clusters even when other facets match.
    """
    if not detail:
        return "n/a"
    val = detail.get("max_abs_diff") or detail.get("abs_diff")
    if val is None:
        return "n/a"
    try:
        v = abs(float(val))
    except (TypeError, ValueError):
        return "n/a"
    if v == 0.0 or not math.isfinite(v):
        return "n/a"
    exp = int(math.floor(math.log10(v)))
    return f"1e{exp:+d}"


def _cluster_signature(d: DiffRecord) -> tuple:
    if d.divergence_kind == "exception_mismatch":
        return (
            "exception_mismatch",
            d.ref.exception_type,
            d.cand.exception_type,
        )
    if d.divergence_kind == "shape":
        det = d.divergence_detail or {}
        return (
            "shape",
            det.get("ref_type") or "",
            det.get("cand_type") or "",
            tuple(det.get("ref_shape") or ()),
            tuple(det.get("cand_shape") or ()),
        )
    if d.divergence_kind == "value":
        det = d.divergence_detail or {}
        return (
            "value",
            det.get("reason") or "numeric_drift",
            _magnitude_bucket(det),
        )
    return (d.divergence_kind,)


def _pick_representative(records: list[DiffRecord]) -> DiffRecord:
    return min(records, key=lambda r: (len(r.inputs_repr), r.inputs_repr))


def cluster_divergences(records: list[DiffRecord]) -> list[DivergenceCluster]:
    if not records:
        return []
    grouped: dict[tuple, list[DiffRecord]] = defaultdict(list)
    for r in records:
        grouped[_cluster_signature(r)].append(r)

    clusters: list[DivergenceCluster] = []
    for idx, (sig, members) in enumerate(sorted(grouped.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))):
        rep = _pick_representative(members)
        clusters.append(
            DivergenceCluster(
                cluster_id=f"C{idx + 1:03d}",
                divergence_kind=rep.divergence_kind,
                member_count=len(members),
                representative=rep,
                signature=sig,
            )
        )
    return clusters
