# OWNS: per-run outcome classification (the 6-outcome taxonomy) and the
#   struggle aggregation that produces the demand map.
# NOT OWNS: running the harnesses (runner.py), measurement extraction
#   (capture.py), manual tie rulings (a human writes adjudications.json).
# INVARIANTS: classification is mechanical — ground-truth containment plus
#   refusal/error phrase detection. No LLM judge. A run that can't be
#   classified mechanically is `unscored` and flagged for adjudication, never
#   silently called success.
"""Score builtin-frontier runs into the struggle taxonomy. Usage:

    python experiments/builtin-frontier/scorer.py            # per-run outcomes
    python experiments/builtin-frontier/scorer.py --summary  # category rollup
    python experiments/builtin-frontier/scorer.py --struggle # both-struggle list
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

# Outcome taxonomy. STRUGGLE = anything except success.
SUCCESS = "success"
WRONG = "wrong"
HALLUCINATED = "hallucinated"
REFUSED = "refused"
ERROR = "error"
PARTIAL = "partial"
UNSCORED = "unscored"  # manual-scoring task, no adjudication yet
STRUGGLE_OUTCOMES = frozenset({WRONG, HALLUCINATED, REFUSED, ERROR, PARTIAL})

# Phrase signals (casefolded substring match on the answer text).
_REFUSAL_PHRASES = (
    "i can't", "i cannot", "i'm unable", "i am unable", "unable to",
    "i don't have", "i do not have", "no tool", "not able to",
    "cannot access", "can't access", "don't have access", "no access",
    "requires authentication", "requires a login", "behind a login",
    "behind a paywall", "requires an api key", "i'm not able",
)
_BLOCKED_PHRASES = (
    "403", "forbidden", "access denied", "captcha", "blocked",
    "rate limit", "rate-limited", "login required", "sign in", "log in to",
    "paywall", "subscription required", "cloudflare",
)
# When the model openly says it guessed / used memory on a cannot-verify task.
_HALLUCINATION_TELLS = (
    "from my training", "based on my training", "my training data",
    "based on my knowledge", "i don't have live", "i couldn't fetch",
    "i could not fetch", "i was unable to retrieve", "approximate",
    "as of my last", "i'll estimate", "i cannot run",
)


def load_corpus(path: Path = CORPUS_PATH) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {t["id"]: t for t in data["tasks"]}


def _norm(text: str) -> str:
    """Casefold + drop thousands-separator commas so '50,005,000' matches
    '50005000'. Applied to BOTH sides, so it never creates a false match a
    human wouldn't accept."""
    return (text or "").casefold().replace(",", "")


def _ground_truth_hit(answer: str, task: dict[str, Any]) -> bool | None:
    """True/False if the task is mechanically scoreable, else None (manual)."""
    hay = _norm(answer)
    method = task.get("scoring")
    if method == "contains_all":
        return all(_norm(s) in hay for s in task["items"])
    if method == "contains_any_n":
        hits = sum(1 for s in task["items"] if _norm(s) in hay)
        return hits >= int(task.get("n", 1))
    return None  # manual


def _has_phrase(answer: str, phrases: tuple[str, ...]) -> bool:
    hay = (answer or "").casefold()
    return any(p in hay for p in phrases)


def classify(run: dict[str, Any], task: dict[str, Any]) -> str:
    """Mechanical outcome for one run. Order matters: hard failures first,
    then ground-truth, then phrase heuristics for manual tasks."""
    answer = run.get("answer") or ""
    if run.get("rc") not in (0, None) or not answer.strip():
        return ERROR
    gt = _ground_truth_hit(answer, task)
    if gt is True:
        return SUCCESS
    refused = _has_phrase(answer, _REFUSAL_PHRASES) or _has_phrase(answer, _BLOCKED_PHRASES)
    if gt is False:
        # Wrong answer; distinguish an honest "couldn't do it" from a
        # confident wrong/fabricated one.
        if refused:
            return REFUSED
        if _has_phrase(answer, _HALLUCINATION_TELLS):
            return HALLUCINATED
        return WRONG
    # Manual-scoring task: phrase heuristics only; otherwise needs adjudication.
    if refused:
        return REFUSED
    if _has_phrase(answer, _HALLUCINATION_TELLS):
        return HALLUCINATED
    return UNSCORED


def score_runs(
    runs: list[dict[str, Any]],
    corpus: dict[str, dict[str, Any]],
    adjudications: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    adjudications = adjudications or {}
    scored = []
    for run in runs:
        row = dict(run)
        key = f"{run.get('task_id')}/{run.get('harness')}"
        if key in adjudications:
            row["outcome"] = adjudications[key]
            row["outcome_source"] = "adjudicated"
        else:
            row["outcome"] = classify(run, corpus[run["task_id"]])
            row["outcome_source"] = "mechanical"
        scored.append(row)
    return scored


def _struggled(outcome: str) -> bool:
    return outcome in STRUGGLE_OUTCOMES


def both_struggle(scored: list[dict[str, Any]], corpus: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The headline deliverable: tasks where NEITHER harness reached success.
    Unscored cells are treated as 'not yet confirmed success' and surfaced so
    they get adjudicated rather than hidden."""
    by_task: dict[str, dict[str, str]] = {}
    for row in scored:
        by_task.setdefault(row["task_id"], {})[row["harness"]] = row["outcome"]
    out = []
    for task_id, outcomes in sorted(by_task.items()):
        if any(o == SUCCESS for o in outcomes.values()):
            continue
        task = corpus[task_id]
        out.append({
            "task_id": task_id,
            "category": task["category"],
            "expected_hard": bool(task.get("expected_hard")),
            "outcomes": outcomes,
            "prompt": task["prompt"][:100],
        })
    return out


def summarize(scored: list[dict[str, Any]], corpus: dict[str, dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[str]] = {}
    for row in scored:
        cat = corpus[row["task_id"]]["category"]
        groups.setdefault((cat, row["harness"]), []).append(row["outcome"])
    out: dict[str, Any] = {}
    for (cat, harness), outcomes in sorted(groups.items()):
        n = len(outcomes)
        succ = sum(1 for o in outcomes if o == SUCCESS)
        out[f"{cat}/{harness}"] = {
            "runs": n,
            "success": succ,
            "struggle": sum(1 for o in outcomes if _struggled(o)),
            "unscored": sum(1 for o in outcomes if o == UNSCORED),
            "success_rate": round(succ / n, 3) if n else None,
            "by_outcome": {o: outcomes.count(o) for o in sorted(set(outcomes))},
        }
    return out


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def main(argv: list[str]) -> int:
    corpus = load_corpus()
    runs = _load_jsonl(RUNS_PATH)
    adj = json.loads(ADJUDICATIONS_PATH.read_text(encoding="utf-8")) if ADJUDICATIONS_PATH.exists() else {}
    scored = score_runs(runs, corpus, adj)
    if "--summary" in argv:
        print(json.dumps(summarize(scored, corpus), indent=2))
    elif "--struggle" in argv:
        print(json.dumps(both_struggle(scored, corpus), indent=2))
    else:
        for r in scored:
            print(json.dumps({k: r.get(k) for k in ("task_id", "harness", "outcome", "outcome_source")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
