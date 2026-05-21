"""quant-bench corpus test — the public quality claim.

# OWNS: running every benchmark entry through the validator, scoring the
#        results, and asserting precision / recall / false-alarm /
#        broken-detection cross the v1 thresholds.
# NOT OWNS: lifecycle / plumbing — see test_quant_patch_validator_lifecycle.
# INVARIANTS:
#   - Thresholds match those documented in benchmarks/quant_bench/README.md.
#     A change here without a corresponding README update is a docs drift
#     bug. Keep them aligned.
# DECISIONS:
#   - We always run the bench in "quick" tier so the test stays under
#     a few minutes. The full corpus at "standard" tier is operator-run,
#     not CI.
#   - This test is marked slow because even at "quick" tier it takes
#     30 s × N entries. Operators run it locally; CI can opt in via
#     `pytest -m slow`.
"""

from __future__ import annotations

import pytest

from benchmarks.quant_bench import score as _score


pytestmark = pytest.mark.slow


def test_quant_bench_corpus_thresholds():
    """Precision / recall / false-alarm / broken-detect must clear v1 gates."""
    report = _score.run_bench(fuzz_budget="quick")
    metrics = report["metrics"]
    thresholds = metrics["thresholds"]

    assert metrics["precision"] >= thresholds["precision_min"], (
        f"precision {metrics['precision']} < {thresholds['precision_min']}; "
        f"per-entry: {report['per_entry']}"
    )
    assert metrics["recall"] >= thresholds["recall_min"], (
        f"recall {metrics['recall']} < {thresholds['recall_min']}; "
        f"per-entry: {report['per_entry']}"
    )
    assert metrics["false_alarm_rate"] <= thresholds["false_alarm_max"], (
        f"false_alarm_rate {metrics['false_alarm_rate']} > "
        f"{thresholds['false_alarm_max']}; per-entry: {report['per_entry']}"
    )
    # Broken-detection rate is not formally a CI gate yet but we record it.
    assert metrics["broken_detection_rate"] >= 0.95, (
        f"broken_detection {metrics['broken_detection_rate']} < 0.95"
    )
