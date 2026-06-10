"""Tests for the deference experiment harness (experiments/deference/).

The scorer is the part whose bugs would silently corrupt the experiment's
conclusions, so it gets the coverage; the runner is exercised end-to-end by
the experiment itself.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_EXP_DIR = Path(__file__).resolve().parent.parent / "experiments" / "deference"

# Load by explicit path under a unique module name. Both experiments ship a
# `scorer.py`; a plain `import scorer` would collide in a shared pytest process
# (whichever test imports first wins the module cache).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("deference_scorer", _EXP_DIR / "scorer.py")
scorer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scorer)


def test_corpus_loads_and_is_well_formed():
    corpus = scorer.load_corpus()
    assert len(corpus) == 16
    categories = {task["category"] for task in corpus.values()}
    assert categories == {"web_scrape", "live_lookup", "sandboxed_exec", "dep_audit"}
    for task_id, task in corpus.items():
        assert task["prompt"].strip(), task_id
        gt = task["ground_truth"]
        assert ("contains_all" in gt) or ("contains_any_n" in gt), task_id


def test_score_answer_contains_all_casefold():
    gt = {"contains_all": [".test", ".example"]}
    assert scorer.score_answer("It lists .TEST and .Example TLDs", gt) == "pass"
    assert scorer.score_answer("It lists only .test", gt) == "fail"
    assert scorer.score_answer("", gt) == "fail"


def test_score_answer_contains_any_n():
    gt = {"contains_any_n": {"n": 2, "items": ["CVE-1", "CVE-2", "CVE-3"]}}
    assert scorer.score_answer("found cve-1 and CVE-3", gt) == "pass"
    assert scorer.score_answer("found CVE-1 only", gt) == "fail"


def test_score_answer_rejects_unknown_shape():
    with pytest.raises(ValueError):
        scorer.score_answer("x", {"regex": "y"})


def _run(task_id="web-01", harness="openclaw", mode="aztea", **kw):
    base = {
        "task_id": task_id, "harness": harness, "mode": mode, "rc": 0,
        "wall_s": 10.0, "answer": ".test .example .invalid .localhost",
        "infeasible": False, "aztea_jobs": [], "deference": {"blocked": 0, "rows": []},
    }
    base.update(kw)
    return base


def test_score_runs_marks_failures_and_infeasible_distinctly():
    corpus = scorer.load_corpus()
    runs = [
        _run(),                                            # pass
        _run(answer="I could not fetch it, but from memory: .test"),  # fail
        _run(rc=1, answer=None),                           # infeasible (error)
        _run(infeasible=True, answer=""),                  # infeasible (timeout)
    ]
    scored = scorer.score_runs(runs, corpus)
    assert [r["score"] for r in scored] == ["pass", "fail", "infeasible", "infeasible"]


def test_score_runs_adjudication_overrides_mechanical():
    corpus = scorer.load_corpus()
    runs = [_run(answer="the four are test / example / invalid / localhost (no dots)")]
    assert scorer.score_runs(runs, corpus)[0]["score"] == "fail"
    adjudicated = scorer.score_runs(
        runs, corpus, {"web-01/openclaw/aztea": "pass"}
    )[0]
    assert adjudicated["score"] == "pass"
    assert adjudicated["score_source"] == "adjudicated"


def test_summarize_groups_by_category_and_counts_spend():
    corpus = scorer.load_corpus()
    runs = [
        _run(aztea_jobs=[{"price_cents": 3}, {"price_cents": 0}]),
        _run(task_id="web-02", answer="links to iana.org/domains/example"),
    ]
    summary = scorer.summarize(scorer.score_runs(runs, corpus), corpus)
    key = "web_scrape/openclaw/aztea"
    assert summary[key]["runs"] == 2
    assert summary[key]["pass"] == 2
    assert summary[key]["aztea_spend_cents"] == 3
    assert summary[key]["deferred_runs"] == 1


def test_exec_ground_truths_are_reproducible():
    # The frozen exec answers must stay derivable — if this fails, the corpus
    # was authored against a different runtime and the experiment is invalid.
    import base64
    import hashlib
    import random
    import statistics

    corpus = scorer.load_corpus()
    assert hashlib.sha256(b"aztea-deference-2026").hexdigest() in corpus["exec-01"]["ground_truth"]["contains_all"]
    random.seed(42)
    assert str(random.randint(1, 10**9)) in corpus["exec-02"]["ground_truth"]["contains_all"]
    blob = corpus["exec-03"]["prompt"].rsplit(": ", 1)[1]
    decoded = base64.b64decode(blob).decode()
    assert corpus["exec-03"]["ground_truth"]["contains_all"][0] in decoded
    vals = [12.5, 18.3, 11.7, 25.4, 19.9, 31.2, 14.8, 22.6, 17.1, 28.0, 13.3, 20.5, 26.7, 15.9, 24.2]
    assert f"{statistics.pstdev(vals):.6f}" in corpus["exec-04"]["ground_truth"]["contains_all"]
