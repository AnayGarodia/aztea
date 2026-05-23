"""Tests for core/hosted_index/ — repo ingest, retrieval, and bug-signal judge."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from core import hosted_index as hi
from core.hosted_index import github_app, store
from core.hosted_index.ingest import _detect_revert_or_hotfix
from core.hosted_index.judge import _severity_from_signals
from core.hosted_index.types import BugSignal, CommitMeta, IngestStatus


# ---------------------------------------------------------------------------
# Pure-helper tests (no DB / git / network)
# ---------------------------------------------------------------------------


def test_detect_revert_via_explicit_sha():
    msg = 'Revert "fix typo"\n\nThis reverts commit abc1234567890.'
    reverted, hotfix = _detect_revert_or_hotfix(msg)
    assert reverted == "abc1234567890"
    assert hotfix is None


def test_detect_hotfix_target():
    msg = "Hotfix for abcdef1: regression in subtract logic"
    reverted, hotfix = _detect_revert_or_hotfix(msg)
    assert hotfix == "abcdef1"


def test_detect_no_signal_returns_none():
    msg = "Add unrelated feature"
    reverted, hotfix = _detect_revert_or_hotfix(msg)
    assert reverted is None
    assert hotfix is None


def test_detect_handles_non_string():
    reverted, hotfix = _detect_revert_or_hotfix(None)  # type: ignore[arg-type]
    assert reverted is None
    assert hotfix is None


# ---------------------------------------------------------------------------
# Severity ladder (pure)
# ---------------------------------------------------------------------------


def test_severity_strong_requires_both_signals():
    s = _severity_from_signals(was_reverted=True, hotfix_count=1, incident_count=1)
    assert s == "strong"
    s = _severity_from_signals(was_reverted=False, hotfix_count=2, incident_count=1)
    assert s == "strong"


def test_severity_moderate_for_single_signal():
    assert _severity_from_signals(was_reverted=True, hotfix_count=0, incident_count=0) == "moderate"
    assert _severity_from_signals(was_reverted=False, hotfix_count=1, incident_count=0) == "moderate"
    assert _severity_from_signals(was_reverted=False, hotfix_count=0, incident_count=1) == "moderate"


def test_severity_none_when_no_signal():
    assert _severity_from_signals(was_reverted=False, hotfix_count=0, incident_count=0) == "none"


# ---------------------------------------------------------------------------
# GitHub App configuration tests (no network)
# ---------------------------------------------------------------------------


def test_github_app_not_configured_returns_false(monkeypatch):
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    assert github_app.is_configured() is False


def test_github_app_missing_id_raises(monkeypatch, tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text("dummy")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    with pytest.raises(github_app.GitHubAppNotConfigured, match="GITHUB_APP_ID"):
        github_app.mint_app_jwt()


def test_github_app_unreadable_key_raises(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY_PATH", "/nonexistent/path.pem")
    monkeypatch.setenv("GITHUB_APP_ID", "12345")
    with pytest.raises(github_app.GitHubAppNotConfigured, match="unreadable"):
        github_app.mint_app_jwt()


def test_installation_id_must_be_positive():
    with pytest.raises(ValueError, match="positive int"):
        github_app.get_installation_token(0)
    with pytest.raises(ValueError, match="positive int"):
        github_app.get_installation_token(-1)


def test_authenticated_clone_url_requires_owner_slash_repo():
    with pytest.raises(ValueError, match="owner/repo"):
        github_app.authenticated_clone_url("not-a-valid-name", 12345)


# ---------------------------------------------------------------------------
# Store CRUD tests (need DB; rely on migration 0065 already applied)
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup_test_repos():
    """Track repo_ids created during a test and delete them on exit."""
    created: list[str] = []
    yield created
    for rid in created:
        try:
            hi.delete_repo(rid)
        except Exception:
            pass


def test_upsert_repo_is_idempotent(cleanup_test_repos):
    rid1 = store.upsert_repo("owner-1", "github://example/foo")
    cleanup_test_repos.append(rid1)
    rid2 = store.upsert_repo("owner-1", "github://example/foo")
    assert rid1 == rid2
    assert store.get_repo(rid1) is not None


def test_upsert_commit_writes_metadata(cleanup_test_repos):
    rid = store.upsert_repo("owner-2", "github://example/bar")
    cleanup_test_repos.append(rid)
    meta = CommitMeta(
        commit_sha="abc123",
        repo_id=rid,
        parent_sha=None,
        author="Test <test@example.com>",
        ts="2026-01-01T00:00:00Z",
    )
    store.upsert_commit(meta)
    row = store.get_commit(rid, "abc123")
    assert row is not None
    assert row["author"] == "Test <test@example.com>"
    assert store.commit_count(rid) == 1


def test_incidents_referencing_finds_match(cleanup_test_repos):
    rid = store.upsert_repo("owner-3", "github://example/baz")
    cleanup_test_repos.append(rid)
    store.add_incident(rid, "DB outage", ["abc123", "def456"])
    matches = store.incidents_referencing(rid, "abc123")
    assert len(matches) == 1
    assert matches[0]["summary"] == "DB outage"


def test_delete_repo_clears_every_table(cleanup_test_repos):
    rid = store.upsert_repo("owner-4", "github://example/quux")
    store.upsert_commit(CommitMeta(
        commit_sha="x", repo_id=rid, parent_sha=None,
        author="A", ts="2026-01-01T00:00:00Z",
    ))
    store.add_incident(rid, "n/a", ["x"])
    deleted = store.delete_repo(rid)
    assert deleted >= 3  # repo + commit + incident at minimum
    assert store.get_repo(rid) is None


# ---------------------------------------------------------------------------
# Judge tests (DB-backed but no git)
# ---------------------------------------------------------------------------


def test_judge_returns_none_for_unknown_commit():
    signal = hi.did_this_change_cause_a_bug("nonexistent-sha", "nonexistent-repo")
    assert signal.severity == "none"
    assert "not in index" in signal.reasons[0]


def test_judge_moderate_when_reverted(cleanup_test_repos):
    rid = store.upsert_repo("owner-judge-1", "github://example/judge1")
    cleanup_test_repos.append(rid)
    store.upsert_commit(CommitMeta(
        commit_sha="bug1", repo_id=rid, parent_sha=None,
        author="A", ts="2026-01-01T00:00:00Z",
        was_reverted=True,
    ))
    signal = hi.did_this_change_cause_a_bug("bug1", rid)
    assert signal.severity == "moderate"
    assert "reverted" in " ".join(signal.reasons)
    assert "bug1" in signal.citations


def test_judge_strong_when_reverted_and_incident_linked(cleanup_test_repos):
    rid = store.upsert_repo("owner-judge-2", "github://example/judge2")
    cleanup_test_repos.append(rid)
    store.upsert_commit(CommitMeta(
        commit_sha="bug2", repo_id=rid, parent_sha=None,
        author="A", ts="2026-01-01T00:00:00Z",
        was_reverted=True,
    ))
    store.add_incident(rid, "outage caused by bug2", ["bug2"])
    signal = hi.did_this_change_cause_a_bug("bug2", rid)
    assert signal.severity == "strong"


# ---------------------------------------------------------------------------
# End-to-end ingest from a local fixture repo (no GitHub App needed)
# ---------------------------------------------------------------------------


def _build_fixture_repo(tmpdir: str) -> tuple[str, str]:
    """Build a tiny repo with a bug + revert + hotfix.

    Returns (repo_path, bug_commit_sha).
    """
    repo_path = os.path.join(tmpdir, "fixture")
    os.makedirs(repo_path)
    cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], check=True)

        with open("hello.py", "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], check=True)

        with open("hello.py", "w") as f:
            f.write("def add(a, b):\n    return a - b\n")
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "refactor (oops)"], check=True)
        bug_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip()

        subprocess.run(["git", "revert", "--no-edit", bug_sha], check=True)
        return repo_path, bug_sha
    finally:
        os.chdir(cwd)


@pytest.fixture
def fixture_repo():
    tmpdir = tempfile.mkdtemp(prefix="aztea-test-fixture-")
    try:
        path, bug_sha = _build_fixture_repo(tmpdir)
        yield path, bug_sha
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_ingest_local_repo_indexes_commits_and_hunks(fixture_repo, cleanup_test_repos):
    repo_path, _ = fixture_repo
    result = hi.ingest_repo(owner_id="test-ingest", source=repo_path)
    cleanup_test_repos.append(result.repo_id)
    assert result.commits_indexed == 3
    assert result.hunks_indexed >= 1
    assert result.head_sha
    assert store.commit_count(result.repo_id) == 3


def test_revert_is_detected_during_ingest(fixture_repo, cleanup_test_repos):
    repo_path, bug_sha = fixture_repo
    result = hi.ingest_repo(owner_id="test-revert", source=repo_path)
    cleanup_test_repos.append(result.repo_id)
    # The bug commit must be marked was_reverted=True by the revert pass.
    bug_row = store.get_commit(result.repo_id, bug_sha)
    assert bug_row is not None
    assert int(bug_row["was_reverted"]) == 1


def test_top_k_finds_similar_hunks(fixture_repo, cleanup_test_repos):
    repo_path, _ = fixture_repo
    result = hi.ingest_repo(owner_id="test-retrieve", source=repo_path)
    cleanup_test_repos.append(result.repo_id)
    hits = hi.top_k_similar_hunks(
        query_text="def add(a, b):\n    return a - b",
        repo_id=result.repo_id,
        k=3,
    )
    assert len(hits) >= 1
    assert all(h.file == "hello.py" for h in hits)
    # Highest hit should have score > 0.5 for a near-identical fragment.
    assert hits[0].score > 0.5


def test_ingest_requires_installation_id_for_github_source():
    with pytest.raises(ValueError, match="installation_id is required"):
        hi.ingest_repo(owner_id="x", source="owner/repo")


def test_ingest_rejects_empty_owner():
    with pytest.raises(ValueError, match="owner_id"):
        hi.ingest_repo(owner_id="", source="/tmp/nope")


def test_ingest_rejects_local_non_git_path(tmp_path):
    with pytest.raises(ValueError, match="not a git repo"):
        hi.ingest_repo(owner_id="x", source=str(tmp_path))
