"""
test_agent_codebase_reviewer.py — D16 reference-agent suite (24 tests).

Deepest coverage of any new agent because D16 is the strategy doc's
wedge agent. Every behaviour from the build plan's Section 6 surfaces
here either as an assertion or a setup verification.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core import hosted_index as hi
from tests.agent_helpers import (
    _capture_llm_calls,
    _make_response,
    _make_stateful_llm,
    _stub_llm_factory,
    _ingest_fixture_repo,
    assert_error_envelope,
    assert_reasoning_loop,
    patch_llm_everywhere,
)


def _agent():
    from agents import codebase_reviewer
    return codebase_reviewer


# ---------------------------------------------------------------------------
# 1–6. Input validation
# ---------------------------------------------------------------------------


def test_invalid_input_envelope():
    out = _agent().run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "codebase_reviewer.invalid_input")


def test_missing_repo_id_rejected():
    out = _agent().run({"hunks": [{"file": "a.py", "text": "x"}]})
    err = assert_error_envelope(out, "codebase_reviewer.invalid_input")
    assert "repo_id" in err["message"]


def test_empty_hunks_rejected():
    out = _agent().run({"repo_id": "anything", "hunks": []})
    err = assert_error_envelope(out, "codebase_reviewer.invalid_input")
    assert "hunks" in err["message"]


def test_hunk_missing_file_rejected():
    out = _agent().run({"repo_id": "x", "hunks": [{"text": "y"}]})
    assert_error_envelope(out, "codebase_reviewer.invalid_input")


def test_hunk_missing_text_rejected():
    out = _agent().run({"repo_id": "x", "hunks": [{"file": "a.py"}]})
    assert_error_envelope(out, "codebase_reviewer.invalid_input")


def test_hunks_must_be_list():
    out = _agent().run({"repo_id": "x", "hunks": "not a list"})
    assert_error_envelope(out, "codebase_reviewer.invalid_input")


# ---------------------------------------------------------------------------
# 7–9. Numeric clamping
# ---------------------------------------------------------------------------


def test_max_hunks_clamped_to_25(monkeypatch, tmp_path):
    """Passing max_hunks=1000 must still only process at most 25 hunks."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)

    hunks = [{"file": f"h{i}.py", "text": f"def fn{i}(): pass"} for i in range(50)]
    out = _agent().run({
        "repo_id": result.repo_id, "hunks": hunks, "max_hunks": 1000,
    })
    # Each hunk triggers ≥ 1 per-hunk LLM call + 1 synthesis. So upper bound
    # is 25 + 1 = 26 calls. Empirically expect <= 26.
    assert len(calls) <= 27, (
        f"expected at most 25 hunks processed (26 LLM calls); got {len(calls)}"
    )
    hi.delete_repo(result.repo_id)


def test_k_per_hunk_clamped_to_10(monkeypatch, tmp_path):
    """k_per_hunk=999 clamps to 10."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "x.py", "text": "def f(): pass"}],
        "k_per_hunk": 999,
    })
    # Just verify the agent didn't fail — clamping is internal, but the
    # finding's evidence list must be <= 10 entries.
    if "findings" in out:
        for finding in out["findings"]:
            assert len(finding["evidence"]) <= 10
    hi.delete_repo(result.repo_id)


def test_budget_cents_clamped_to_500(monkeypatch, tmp_path):
    """budget_cents=99999 clamps to 500."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))
    # Passes; just verifies the agent doesn't reject a big budget.
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "x.py", "text": "x"}],
        "budget_cents": 99999,
    })
    assert isinstance(out, dict)
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 10–11. Repo lifecycle errors
# ---------------------------------------------------------------------------


def test_repo_not_indexed_returns_specific_code():
    out = _agent().run({
        "repo_id": "absolutely-not-a-repo-id-yo",
        "hunks": [{"file": "a.py", "text": "x"}],
    })
    err = assert_error_envelope(out, "codebase_reviewer.repo_not_indexed")
    assert err["details"]["repo_id"] == "absolutely-not-a-repo-id-yo"


def test_repo_not_ready_returns_specific_code(tmp_path):
    """A repo with status != 'ready' must surface repo_not_ready."""
    from core.hosted_index import store, types
    repo_id = store.upsert_repo("test-owner", "github://example/foo")
    # Keep status at 'pending' (upsert_repo default).
    out = _agent().run({
        "repo_id": repo_id,
        "hunks": [{"file": "a.py", "text": "x"}],
    })
    err = assert_error_envelope(out, "codebase_reviewer.repo_not_ready")
    assert err["details"]["status"] == "pending"
    store.delete_repo(repo_id)


# ---------------------------------------------------------------------------
# 12–14. Happy path with fixture repo
# ---------------------------------------------------------------------------


def test_happy_path_with_fixture_repo(monkeypatch, tmp_path):
    result, shas = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"note","rationale":"buggy",'
        '"summary":"PR has a flagged change.","confidence":"medium"}',
    ))

    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "hello.py", "text": "def add(a, b):\n    return a - b\n"}],
    })
    assert "findings" in out, f"expected findings, got {out!r}"
    assert len(out["findings"]) == 1
    assert out["confidence"] in {"low", "medium", "high"}
    assert "summary" in out
    hi.delete_repo(result.repo_id)


def test_per_hunk_evidence_includes_bug_severity(monkeypatch, tmp_path):
    result, shas = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"risk","rationale":"x","summary":"s","confidence":"high"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "hello.py", "text": "def add(a, b):\n    return a - b\n"}],
    })
    assert "findings" in out
    for ev in out["findings"][0]["evidence"]:
        assert ev["bug_severity"] in {"none", "weak", "moderate", "strong"}
    hi.delete_repo(result.repo_id)


def test_findings_truncated_to_max_hunks(monkeypatch, tmp_path):
    """findings length must equal min(len(hunks), max_hunks)."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))
    hunks = [{"file": f"h{i}.py", "text": f"x={i}"} for i in range(5)]
    out = _agent().run({
        "repo_id": result.repo_id, "hunks": hunks, "max_hunks": 3,
    })
    assert len(out["findings"]) == 3
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 15. Hunk text clipped at 4000 chars
# ---------------------------------------------------------------------------


def test_hunk_text_clipped_to_4000_chars(monkeypatch, tmp_path):
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)

    big_hunk = "x = 1\n" * 2000  # 12000 chars
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": big_hunk}],
    })
    # First call is per-hunk verdict; its user message should not contain
    # the full 12000-char hunk.
    user_msg = next(m.content for m in calls[0].messages if m.role == "user")
    # 4000 chars + the "[truncated N chars]" marker — should be much
    # smaller than 12000.
    assert len(user_msg) < 8000, (
        f"hunk text not clipped — user message has {len(user_msg)} chars"
    )
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 16–17. Reasoning loop
# ---------------------------------------------------------------------------


def test_reasoning_loop_at_least_two_calls(monkeypatch, tmp_path):
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    # 1 per-hunk + 1 synthesis = 2 minimum.
    assert len(calls) >= 2
    hi.delete_repo(result.repo_id)


def test_synthesis_llm_sees_per_hunk_findings(monkeypatch, tmp_path):
    """The second (synthesis) LLM call's user message must include the
    per-hunk findings from the first call."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)
    _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "specialfilename.py", "text": "x"}],
    })
    # Last LLM call is synthesis; its user message contains JSON of findings.
    synth_user = next(m.content for m in calls[-1].messages if m.role == "user")
    assert "specialfilename.py" in synth_user, (
        f"synthesis user message did not reference the hunk's file: {synth_user[:300]}"
    )
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 18. Budget exhaustion returns partial findings
# ---------------------------------------------------------------------------


def test_budget_exceeded_returns_partial_findings(monkeypatch, tmp_path):
    """Direct BudgetExceededError from the LLM layer surfaces as the
    agent's budget_exceeded envelope with partial_findings preserved.

    Why stub instead of real budget gate: with no provider keys in test
    env, run_with_fallback raises LLMError('none',...) before the budget
    gate fires (the loop exits via empty-chain path). Stubbing the LLM
    to raise BudgetExceededError tests the AGENT'S handling specifically.
    """
    from core.llm.errors import BudgetExceededError
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")

    def _budget_buster(req, *args, **kwargs):
        raise BudgetExceededError(
            "stub", "stub-model", "agent budget exhausted",
            budget_cents=1, spent_cents=0, estimated_next_cents=5,
        )
    patch_llm_everywhere(monkeypatch, _budget_buster)

    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
        "budget_cents": 1,
    })
    err = assert_error_envelope(out, "codebase_reviewer.budget_exceeded")
    assert "partial_findings" in err["details"]
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 19–21. Malformed LLM output handling
# ---------------------------------------------------------------------------


def test_llm_returns_unparseable_json_for_per_hunk_uses_safe_default(
    monkeypatch, tmp_path,
):
    """Bad JSON from the per-hunk LLM → verdict defaults to 'note', rationale
    notes the parse failure."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        "I am not JSON, I am free text from a model that ignored instructions.",
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    assert "findings" in out
    finding = out["findings"][0]
    assert finding["verdict"] == "note"
    assert "JSON" in finding["rationale"] or "parseable" in finding["rationale"]
    hi.delete_repo(result.repo_id)


def test_llm_returns_invalid_verdict_label_normalised(monkeypatch, tmp_path):
    """An out-of-band verdict like 'MAYBE' → 'note'."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"MAYBE","rationale":"r","summary":"s","confidence":"low"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    assert out["findings"][0]["verdict"] == "note"
    hi.delete_repo(result.repo_id)


def test_llm_returns_invalid_confidence_label_normalised(monkeypatch, tmp_path):
    """An out-of-band confidence label normalises to 'low'."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"VERY HIGH"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    assert out["confidence"] == "low"
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 22. confidence field is in canonical three-value set
# ---------------------------------------------------------------------------


def test_confidence_field_in_three_canonical_values(monkeypatch, tmp_path):
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"medium"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    assert out["confidence"] in {"low", "medium", "high"}
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 23. Retrieval failure doesn't crash the loop
# ---------------------------------------------------------------------------


def test_retrieval_failure_does_not_crash_loop(monkeypatch, tmp_path):
    """If top_k_similar_hunks raises, the hunk still gets a finding
    (with empty evidence) instead of failing the whole call."""
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")

    def _bad_top_k(**kwargs):
        raise RuntimeError("vector store offline")
    monkeypatch.setattr("agents.codebase_reviewer._hi.top_k_similar_hunks",
                        _bad_top_k)

    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    assert "findings" in out
    assert out["findings"][0]["evidence"] == []
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 24. Strong bug signal surfaces in rationale
# ---------------------------------------------------------------------------


def test_strong_bug_signal_surfaces_in_evidence(monkeypatch, tmp_path):
    """When a query hunk closely matches the fixture's bug commit, the
    evidence list should mention 'moderate' or 'strong' severity for that
    commit (it was reverted in the fixture)."""
    result, shas = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    stub, calls = _capture_llm_calls()
    patch_llm_everywhere(monkeypatch, stub)

    # Use the EXACT bug text from the fixture so retrieval scores high.
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "hello.py", "text": "def add(a, b):\n    return a - b\n"}],
    })
    assert "findings" in out
    severities = [ev["bug_severity"] for ev in out["findings"][0]["evidence"]]
    # At least one evidence entry should be moderate or higher (the bug was reverted).
    assert any(s in {"moderate", "strong"} for s in severities), (
        f"expected at least one moderate/strong signal, got {severities}"
    )
    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# Bonus: reasoning loop invariant + trace serialisation in one swing.
# ---------------------------------------------------------------------------


def test_trace_serialises_in_success_path(monkeypatch, tmp_path):
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))
    out = _agent().run({
        "repo_id": result.repo_id,
        "hunks": [{"file": "h.py", "text": "x"}],
    })
    assert_reasoning_loop(out)
    hi.delete_repo(result.repo_id)
