"""Tests for the builtin-frontier experiment (experiments/builtin-frontier/).

The scorer's outcome taxonomy and the corpus's well-formedness are what would
silently corrupt the demand-map conclusion, so they get the coverage.
"""
from __future__ import annotations

import sys
from pathlib import Path

_EXP = Path(__file__).resolve().parent.parent / "experiments" / "builtin-frontier"

# Load by explicit path under a unique module name. Both experiments ship a
# `scorer.py`; a plain `import scorer` would collide in a shared pytest process
# (whichever test imports first wins the module cache).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("frontier_scorer", _EXP / "scorer.py")
scorer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scorer)


def test_corpus_well_formed():
    corpus = scorer.load_corpus()
    assert len(corpus) == 72
    cats: dict[str, int] = {}
    for t in corpus.values():
        cats[t["category"]] = cats.get(t["category"], 0) + 1
    assert len(cats) == 12
    assert all(n == 6 for n in cats.values()), cats
    for tid, t in corpus.items():
        assert t["prompt"].strip(), tid
        assert t["scoring"] in ("contains_all", "contains_any_n", "manual"), tid
        if t["scoring"] != "manual":
            assert t["items"], tid


def test_expected_hard_count_in_range():
    corpus = scorer.load_corpus()
    hard = [t for t in corpus.values() if t.get("expected_hard")]
    # ~13-15 per the plan; all of web_walled (6) plus OCR/infra/perf subsets.
    assert 12 <= len(hard) <= 16, len(hard)
    assert all(t["expected_hard"] for t in corpus.values() if t["category"] == "web_walled")


def _run(task_id, harness="openclaw", answer="", rc=0):
    return {"task_id": task_id, "harness": harness, "answer": answer, "rc": rc}


def test_classify_success_on_ground_truth():
    corpus = scorer.load_corpus()
    # nc-04 ground truth is 50005000.
    assert scorer.classify(_run("nc-04", answer="The sum is 50005000."), corpus["nc-04"]) == scorer.SUCCESS


def test_ground_truth_is_comma_insensitive():
    corpus = scorer.load_corpus()
    # Models format big numbers with thousands separators; both sides normalize.
    assert scorer.classify(_run("nc-04", answer="The answer is **50,005,000**."), corpus["nc-04"]) == scorer.SUCCESS
    assert scorer.classify(_run("de-04", answer="Q3 revenue is $147,300."), corpus["de-04"]) == scorer.SUCCESS


def test_classify_wrong_vs_hallucinated_vs_refused():
    corpus = scorer.load_corpus()
    t = corpus["ll-02"]  # express latest = 5.2.1
    assert scorer.classify(_run("ll-02", answer="It is 4.18.2"), t) == scorer.WRONG
    assert scorer.classify(
        _run("ll-02", answer="Based on my training data it's around 4.18"), t
    ) == scorer.HALLUCINATED
    assert scorer.classify(
        _run("ll-02", answer="I cannot access the npm registry from here."), t
    ) == scorer.REFUSED


def test_classify_error_on_empty_or_nonzero_rc():
    corpus = scorer.load_corpus()
    assert scorer.classify(_run("nc-04", answer="", rc=0), corpus["nc-04"]) == scorer.ERROR
    assert scorer.classify(_run("nc-04", answer="50005000", rc=1), corpus["nc-04"]) == scorer.ERROR


def test_classify_manual_task_unscored_unless_phrase():
    corpus = scorer.load_corpus()
    walled = corpus["ww-01"]  # manual, expected_hard
    assert scorer.classify(_run("ww-01", answer="Here is a plausible summary..."), walled) == scorer.UNSCORED
    assert scorer.classify(
        _run("ww-01", answer="This requires a login, I can't access it."), walled
    ) == scorer.REFUSED


def test_both_struggle_excludes_any_success():
    corpus = scorer.load_corpus()
    runs = [
        _run("nc-04", "openclaw", "50005000"),   # success
        _run("nc-04", "hermes", "wrong"),        # struggle
        _run("oc-01", "openclaw", "I can't read images"),  # refused
        _run("oc-01", "hermes", "no OCR tool"),  # struggle (manual→unscored, but no success)
    ]
    scored = scorer.score_runs(runs, corpus)
    struggle = {r["task_id"] for r in scorer.both_struggle(scored, corpus)}
    assert "nc-04" not in struggle   # openclaw succeeded
    assert "oc-01" in struggle       # neither succeeded


def test_adjudication_overrides():
    corpus = scorer.load_corpus()
    runs = [_run("ww-03", "hermes", "London is about 15C")]
    base = scorer.score_runs(runs, corpus)[0]
    assert base["outcome"] == scorer.UNSCORED
    adj = scorer.score_runs(runs, corpus, {"ww-03/hermes": scorer.HALLUCINATED})[0]
    assert adj["outcome"] == scorer.HALLUCINATED and adj["outcome_source"] == "adjudicated"


def test_ground_truths_reproducible():
    # Frozen compute answers must stay derivable, else the corpus is invalid.
    import hashlib
    import math
    import statistics

    corpus = scorer.load_corpus()
    assert hashlib.sha256(b"builtin-frontier-2026").hexdigest() in corpus["nc-01"]["items"]
    assert str(math.factorial(20)) in corpus["nc-05"]["items"]
    primes = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]
    assert f"{statistics.pstdev(primes):.6f}" in corpus["nc-03"]["items"]
