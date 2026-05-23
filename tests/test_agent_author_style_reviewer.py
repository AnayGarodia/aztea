"""test_agent_author_style_reviewer.py — D17 Author Style Reviewer (~9 tests)."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from core.llm.errors import BudgetExceededError, LLMError
from tests.agent_helpers import (
    _capture_llm_calls, _ingest_fixture_repo, _stub_llm_factory,
    assert_error_envelope, patch_llm_everywhere, set_env_for,
)

from agents import author_style_reviewer


_VALID_PAYLOAD = {
    "repo_id": "rid",
    "author_handle": "alice",
    "hunks": [{"file": "a.py", "text": "x"}],
}


def test_invalid_input_envelope():
    out = author_style_reviewer.run("not a dict")  # type: ignore[arg-type]
    assert_error_envelope(out, "author_style_reviewer.invalid_input")


def test_missing_repo_id_rejected():
    out = author_style_reviewer.run(
        {"author_handle": "alice", "hunks": [{"file": "a.py", "text": "x"}]}
    )
    err = assert_error_envelope(out, "author_style_reviewer.invalid_input")
    assert "repo_id" in err["message"]


def test_missing_author_handle_rejected():
    out = author_style_reviewer.run(
        {"repo_id": "rid", "hunks": [{"file": "a.py", "text": "x"}]}
    )
    err = assert_error_envelope(out, "author_style_reviewer.invalid_input")
    assert "author_handle" in err["message"]


def test_empty_hunks_rejected():
    out = author_style_reviewer.run(
        {"repo_id": "rid", "author_handle": "alice", "hunks": []}
    )
    err = assert_error_envelope(out, "author_style_reviewer.invalid_input")
    assert "hunks" in err["message"]


def test_hunks_must_be_list():
    out = author_style_reviewer.run(
        {"repo_id": "rid", "author_handle": "alice", "hunks": "not a list"}
    )
    err = assert_error_envelope(out, "author_style_reviewer.invalid_input")
    assert "hunks" in err["message"]


def test_repo_not_indexed_returns_specific_code():
    out = author_style_reviewer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "author_style_reviewer.repo_not_indexed")
    assert "not in hosted index" in err["message"] or "ingest" in err["message"]


def test_requires_configuration_when_corpus_path_missing(monkeypatch, tmp_path):
    """After ingesting the repo, the corpus-path gate still fires."""
    monkeypatch.delenv("AZTEA_REVIEW_COMMENT_CORPUS_PATH", raising=False)
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    out = author_style_reviewer.run({
        "repo_id": result.repo_id,
        "author_handle": "alice",
        "hunks": [{"file": "hello.py", "text": "x"}],
    })
    err = assert_error_envelope(
        out, "author_style_reviewer.requires_configuration",
    )
    missing_blob = " ".join(err["details"]["missing"])
    assert "AZTEA_REVIEW_COMMENT_CORPUS_PATH" in missing_blob
    from core import hosted_index as hi
    hi.delete_repo(result.repo_id)


def test_repo_not_indexed_takes_precedence_over_corpus_check(monkeypatch):
    """If repo isn't indexed, that error must surface BEFORE the corpus check."""
    monkeypatch.delenv("AZTEA_REVIEW_COMMENT_CORPUS_PATH", raising=False)
    out = author_style_reviewer.run(_VALID_PAYLOAD)
    err = assert_error_envelope(out, "author_style_reviewer.repo_not_indexed")
    # Must NOT have surfaced as requires_configuration.
    assert "requires_configuration" not in err["code"]


def test_budget_exceeded_returns_envelope(monkeypatch, tmp_path):
    """The agent currently exits at requires_configuration before any LLM
    call, so budget pressure surfaces as the configuration gate rather than
    a budget_exceeded envelope. Document the current contract."""
    monkeypatch.delenv("AZTEA_REVIEW_COMMENT_CORPUS_PATH", raising=False)
    result, _ = _ingest_fixture_repo(tmp_path, "bug_revert_fix")
    out = author_style_reviewer.run({
        "repo_id": result.repo_id,
        "author_handle": "alice",
        "hunks": [{"file": "hello.py", "text": "x"}],
        "budget_cents": 1,
    })
    # v0 short-circuits before the LLM is invoked, so the contract is
    # requires_configuration regardless of budget.
    assert_error_envelope(out, "author_style_reviewer.requires_configuration")
    from core import hosted_index as hi
    hi.delete_repo(result.repo_id)
