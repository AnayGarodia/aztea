# OWNS: mechanical pass/fail scoring of experiment answers against the frozen
#   corpus ground truth, and the aggregation used by REPORT.md.
# NOT OWNS: running the harnesses (runner.py), cost capture (capture.py),
#   adjudication of ties (a human writes adjudications.json).
# INVARIANTS: scoring is PURE string matching — no LLM judge, no fuzzy
#   similarity. A run that cannot be scored mechanically is marked "tie", never
#   silently passed or failed.
"""Score experiment runs. Usage:

    python experiments/deference/scorer.py            # score results/runs.jsonl
    python experiments/deference/scorer.py --summary  # per-category rollup
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
CORPUS_PATH = _HERE / "corpus.json"
RUNS_PATH = _HERE / "results" / "runs.jsonl"
ADJUDICATIONS_PATH = _HERE / "adjudications.json"


def load_corpus(path: Path = CORPUS_PATH) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {task["id"]: task for task in data["tasks"]}


def score_answer(answer: str, ground_truth: dict[str, Any]) -> str:
    """Mechanical verdict: 'pass' | 'fail'. Case-insensitive containment —
    deliberately dumb so the scorer itself can't be a source of bias."""
    haystack = (answer or "").casefold()
    contains_all = ground_truth.get("contains_all")
    if isinstance(contains_all, list):
        return "pass" if all(s.casefold() in haystack for s in contains_all) else "fail"
    any_n = ground_truth.get("contains_any_n")
    if isinstance(any_n, dict):
        hits = sum(1 for s in any_n.get("items", []) if s.casefold() in haystack)
        return "pass" if hits >= int(any_n.get("n", 1)) else "fail"
    raise ValueError(f"unknown ground_truth shape: {sorted(ground_truth)}")


def _adjudicated(run: dict[str, Any], adjudications: dict[str, str]) -> str | None:
    return adjudications.get(f"{run.get('task_id')}/{run.get('harness')}/{run.get('mode')}")


def score_runs(
    runs: list[dict[str, Any]],
    corpus: dict[str, dict[str, Any]],
    adjudications: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Attach a 'score' to each run row. Infeasible/errored runs score
    'infeasible' (a first-class outcome — Hermes built-in web is expected to
    land here). Manual adjudications override, keyed task/harness/mode."""
    adjudications = adjudications or {}
    scored = []
    for run in runs:
        row = dict(run)
        manual = _adjudicated(run, adjudications)
        if manual is not None:
            row["score"] = manual
            row["score_source"] = "adjudicated"
        elif run.get("infeasible") or run.get("rc") != 0 or not (run.get("answer") or "").strip():
            row["score"] = "infeasible"
            row["score_source"] = "mechanical"
        else:
            task = corpus[run["task_id"]]
            row["score"] = score_answer(run["answer"], task["ground_truth"])
            row["score_source"] = "mechanical"
        scored.append(row)
    return scored


def _job_spend_cents(run: dict[str, Any]) -> int:
    return sum(int(j.get("price_cents") or 0) for j in run.get("aztea_jobs") or [])


def summarize(scored: list[dict[str, Any]], corpus: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Per (category, harness, mode) rollup: correctness rate, median wall
    time, total aztea spend, deference funnel counts. Pure."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in scored:
        cat = corpus[row["task_id"]]["category"]
        groups.setdefault((cat, row["harness"], row["mode"]), []).append(row)
    out: dict[str, Any] = {}
    for (cat, harness, mode), rows in sorted(groups.items()):
        walls = sorted(r["wall_s"] for r in rows if isinstance(r.get("wall_s"), (int, float)))
        scoreable = [r for r in rows if r["score"] in ("pass", "fail")]
        out[f"{cat}/{harness}/{mode}"] = {
            "runs": len(rows),
            "pass": sum(1 for r in rows if r["score"] == "pass"),
            "fail": sum(1 for r in rows if r["score"] == "fail"),
            "infeasible": sum(1 for r in rows if r["score"] == "infeasible"),
            "correct_rate": (
                round(sum(1 for r in scoreable if r["score"] == "pass") / len(scoreable), 3)
                if scoreable else None
            ),
            "median_wall_s": walls[len(walls) // 2] if walls else None,
            "aztea_spend_cents": sum(_job_spend_cents(r) for r in rows),
            "deferred_runs": sum(1 for r in rows if r.get("aztea_jobs")),
        }
    return out


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def main(argv: list[str]) -> int:
    corpus = load_corpus()
    runs = _load_jsonl(RUNS_PATH)
    adjudications = (
        json.loads(ADJUDICATIONS_PATH.read_text(encoding="utf-8"))
        if ADJUDICATIONS_PATH.exists() else {}
    )
    scored = score_runs(runs, corpus, adjudications)
    if "--summary" in argv:
        print(json.dumps(summarize(scored, corpus), indent=2))
    else:
        for row in scored:
            print(json.dumps({k: row[k] for k in ("task_id", "harness", "mode", "score", "wall_s")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
