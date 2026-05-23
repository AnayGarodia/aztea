"""
types.py — dataclasses for the hosted code index.

# OWNS: HunkMatch, BugSignal, CommitMeta, IngestResult, IngestStatus.
# NOT OWNS: storage I/O (store.py), retrieval (retrieve.py), git walking (ingest.py).
#
# All dataclasses here are frozen so they can be shared across threads
# and passed into reasoning traces without defensive copies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class IngestStatus(str, Enum):
    """Lifecycle of a repo's index entry. Stored as TEXT in repo_index.status."""

    PENDING = "pending"
    INGESTING = "ingesting"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class CommitMeta:
    """Per-commit metadata extracted during ingest."""

    commit_sha: str
    repo_id: str
    parent_sha: str | None
    author: str
    ts: str  # ISO-8601 UTC, 'Z'-suffixed
    was_reverted: bool = False
    hotfix_for: str | None = None
    message: str = ""


@dataclass(frozen=True)
class HunkMatch:
    """One similarity hit from retrieve.top_k_similar_hunks()."""

    hunk_id: str
    repo_id: str
    commit_sha: str
    file: str
    score: float  # cosine in [-1, 1]
    ast_shape_hash: str | None = None


# Signal severity: how strongly we believe this past commit caused a later bug.
BugSeverity = Literal["none", "weak", "moderate", "strong"]


@dataclass(frozen=True)
class BugSignal:
    """Result of judge.did_this_change_cause_a_bug.

    Why a signal not a boolean: judging "did this cause a bug" is inherently
    fuzzy. Returning a graded signal with citations lets the reviewer agent
    weight it appropriately, instead of treating every revert as a smoking
    gun.
    """

    severity: BugSeverity
    citations: tuple[str, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class IngestResult:
    """Summary returned by ingest.ingest_repo() on completion."""

    repo_id: str
    head_sha: str
    commits_indexed: int
    hunks_indexed: int
    skipped: int
    duration_ms: int
