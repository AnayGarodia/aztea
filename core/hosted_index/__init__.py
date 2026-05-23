"""
hosted_index — namespaced code index over a customer's repo for the
D-family agents (D16 Codebase Reviewer first; D17 D18 D19 D20 reuse).

Public surface:
    ingest_repo(owner_id, source, installation_id=None, repo_id=None) → IngestResult
    top_k_similar_hunks(query_text, repo_id, k=10, ...) → list[HunkMatch]
    did_this_change_cause_a_bug(commit_sha, repo_id) → BugSignal
    delete_repo(repo_id) → int

Implementation lives in sibling modules; this file is the import surface.
"""

from __future__ import annotations

from core.hosted_index.ingest import ingest_repo
from core.hosted_index.judge import did_this_change_cause_a_bug
from core.hosted_index.retrieve import top_k_similar_hunks
from core.hosted_index.store import delete_repo
from core.hosted_index.types import (
    BugSeverity,
    BugSignal,
    CommitMeta,
    HunkMatch,
    IngestResult,
    IngestStatus,
)

__all__ = [
    "ingest_repo",
    "top_k_similar_hunks",
    "did_this_change_cause_a_bug",
    "delete_repo",
    "BugSeverity",
    "BugSignal",
    "CommitMeta",
    "HunkMatch",
    "IngestResult",
    "IngestStatus",
]
