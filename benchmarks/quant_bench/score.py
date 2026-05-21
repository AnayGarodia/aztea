"""Score the quant_patch_validator against the bench corpus.

Run me with:    python -m benchmarks.quant_bench.score

Prints a JSON report to stdout containing precision / recall / false-alarm
rate plus a per-entry breakdown. Used by `tests/integration/
test_quant_patch_validator_corpus.py` as the CI gate.

# OWNS: orchestrating one validator call per (entry, candidate) pair and
#        rolling the per-case verdicts into corpus-level metrics.
# NOT OWNS: anything about the validator's internals.
# INVARIANTS:
#   - We never silently skip a candidate. A validator crash on one case
#     is recorded as a failed verdict but never aborts the run.
# DECISIONS:
#   - Default fuzz_budget is "quick" — keeps the bench fast (~5 min total).
#     Override via env var QUANT_BENCH_FUZZ_BUDGET when running deep
#     validation.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from benchmarks.quant_bench.loader import Entry, iter_entries


def _verdict_from_validator_output(out: dict[str, Any]) -> str:
    """Reduce validator output to one of {approved, regression, broken, error}.

    Maps:
    - explicit `error` envelope → 'error'
    - signature_divergence finding (pre-fuzz arity/name mismatch) → 'broken'
    - top-level verdict == 'contract_broken' (return type / shape mismatch) → 'broken'
    - any item in confirmed_regressions → 'regression'
    - otherwise → 'approved'
    """
    if isinstance(out, dict) and "error" in out:
        return "error"
    if not isinstance(out, dict):
        return "error"
    if out.get("signature_divergence"):
        return "broken"
    if out.get("verdict") == "contract_broken":
        return "broken"
    if out.get("confirmed_regressions"):
        return "regression"
    return "approved"


def _score_one(entry: Entry, fuzz_budget: str, fuzz_seconds: float | None) -> dict[str, Any]:
    from agents.quant_patch_validator import run as validator_run  # local import

    rows = []
    for cand in entry.candidates:
        started = time.time()
        payload: dict[str, Any] = {
            "reference_code": entry.reference_source,
            "candidate_code": cand.source,
            "fuzz_budget": fuzz_budget,
        }
        if fuzz_seconds is not None:
            payload["fuzz_seconds"] = fuzz_seconds
        try:
            out = validator_run(payload)
        except Exception as exc:  # noqa: BLE001 — bench must not bail
            out = {"error": {"code": "validator_crash", "message": str(exc)[:600]}}
        verdict = _verdict_from_validator_output(out)
        rows.append(
            {
                "candidate": cand.filename,
                "label": cand.label,
                "verdict": verdict,
                "elapsed_s": round(time.time() - started, 2),
            }
        )
    return {"slug": entry.slug, "category": entry.category, "candidates": rows}


def _aggregate(per_entry: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute precision / recall / false-alarm / broken-detection.

    Counting rules:
    - For a `correct` candidate, ANY non-approved verdict counts as a
      false alarm. We dropped the original score.py's broken-as-zero
      path because it silently masked legitimate misclassifications.
    """
    tp_regression = fn_regression = 0
    false_alarms = 0
    tp_correct = 0
    broken_total = broken_caught = 0
    for entry in per_entry:
        for row in entry["candidates"]:
            label, verdict = row["label"], row["verdict"]
            if label == "regression":
                if verdict == "regression":
                    tp_regression += 1
                else:
                    fn_regression += 1
            elif label == "correct":
                if verdict == "approved":
                    tp_correct += 1
                else:
                    # Any non-approved verdict (regression, broken,
                    # signature_divergence, error) is a false alarm.
                    false_alarms += 1
            elif label == "broken":
                broken_total += 1
                if verdict == "broken":
                    broken_caught += 1

    approved_count = sum(
        1 for e in per_entry for r in e["candidates"] if r["verdict"] == "approved"
    )
    approved_and_correct = sum(
        1
        for e in per_entry
        for r in e["candidates"]
        if r["verdict"] == "approved" and r["label"] == "correct"
    )
    precision = (approved_and_correct / approved_count) if approved_count else float("nan")
    regression_total = tp_regression + fn_regression
    recall = (tp_regression / regression_total) if regression_total else float("nan")
    correct_total = tp_correct + false_alarms
    false_alarm_rate = (false_alarms / correct_total) if correct_total else 0.0
    broken_rate = (broken_caught / broken_total) if broken_total else float("nan")

    return {
        "precision": precision,
        "recall": recall,
        "false_alarm_rate": false_alarm_rate,
        "broken_detection_rate": broken_rate,
        "counts": {
            "approved": approved_count,
            "approved_and_correct": approved_and_correct,
            "regressions_total": regression_total,
            "regressions_caught": tp_regression,
            "correct_total": correct_total,
            "false_alarms": false_alarms,
            "broken_total": broken_total,
            "broken_caught": broken_caught,
        },
        "thresholds": {"precision_min": 0.95, "recall_min": 0.80, "false_alarm_max": 0.05},
    }


def run_bench(
    fuzz_budget: str | None = None,
    fuzz_seconds: float | None = None,
) -> dict[str, Any]:
    budget = fuzz_budget or os.environ.get("QUANT_BENCH_FUZZ_BUDGET", "quick")
    seconds = fuzz_seconds
    if seconds is None:
        env_seconds = os.environ.get("QUANT_BENCH_FUZZ_SECONDS")
        if env_seconds:
            try:
                seconds = float(env_seconds)
            except ValueError:
                seconds = None
    per_entry = [_score_one(e, budget, seconds) for e in iter_entries()]
    metrics = _aggregate(per_entry)
    return {
        "metrics": metrics,
        "per_entry": per_entry,
        "fuzz_budget": budget,
        "fuzz_seconds": seconds,
    }


if __name__ == "__main__":
    report = run_bench()
    print(json.dumps(report, indent=2))
    m = report["metrics"]
    fail = (
        m["precision"] < m["thresholds"]["precision_min"]
        or m["recall"] < m["thresholds"]["recall_min"]
        or m["false_alarm_rate"] > m["thresholds"]["false_alarm_max"]
    )
    sys.exit(1 if fail else 0)
