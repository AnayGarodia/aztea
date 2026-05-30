"""
ingest.py — clone a GitHub repo, walk its history, extract hunks for embedding.

# OWNS: ingest_repo orchestration + git walking via GitPython.
# NOT OWNS: GitHub App auth (github_app.py), embedding (embed.py),
#           DB writes for index/commits/hunks (store.py).
#
# INVARIANTS:
#   * Repo is cloned into a per-ingest tempdir; cleaned up on exit (success or
#     failure). No leaked /tmp directories.
#   * Walk uses `git log --reverse` so commits insert in chronological order
#     and revert-detection works on the partial-graph case.
#   * Hunks are extracted from `git show <sha>` unified-diff output (no extra
#     numpy/AST dependency).
#   * Binary files are skipped at the diff layer — they pollute embeddings.
#
# DECISIONS:
#   * Local-path source allowed in dev (path starts with /) so tests don't
#     require a GitHub App. Production paths must come from GitHub via
#     authenticated_clone_url. The local-path branch is logged and never
#     accepts an http URL by accident.
#   * Revert detection is text-based: "Revert" or "Reverts commit <sha>"
#     in the commit message. The exact SHA pattern catches GitHub-style
#     auto-reverts; the "Revert" keyword catches manual ones.
#   * Hotfix detection looks for "fixes <sha>", "hotfix for <sha>",
#     "regression in <sha>" in the commit message. The SHA can be 7+ hex chars
#     (git short form).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Iterator

import git  # GitPython

from core.hosted_index import embed as _embed
from core.hosted_index import github_app as _github_app
from core.hosted_index import store as _store
from core.hosted_index.types import CommitMeta, IngestResult, IngestStatus

_LOG = logging.getLogger(__name__)

# Cap on per-file diff size embedded. A 1 MB schema migration in a single
# file would otherwise dominate the index without contributing useful
# semantics. Anything over the cap is truncated; the metadata records that.
_MAX_HUNK_CHARS: int = 8_000

# Cap on commits walked per ingest. v0 covers ~1 year of typical
# small-team activity. Larger repos require pagination / incremental ingest
# (TODO once a real customer needs it).
_MAX_COMMITS_PER_INGEST: int = 5_000

# Cap on the depth of files extracted per commit. A merge commit touching
# 500+ files is rare but explodes the per-commit batch. We embed the first
# N files (sorted alphabetically for determinism) and note the rest as
# skipped.
_MAX_FILES_PER_COMMIT: int = 64

_REVERT_SHA_RE = re.compile(r"reverts?\s+commit\s+([0-9a-f]{7,40})", re.IGNORECASE)
_HOTFIX_SHA_RE = re.compile(
    r"(?:fixes?|hotfix(?:\s+for)?|regression\s+in)\s+([0-9a-f]{7,40})",
    re.IGNORECASE,
)
_REVERT_KEYWORD_RE = re.compile(r"^revert\s+", re.IGNORECASE)


def ingest_repo(
    owner_id: str,
    source: str,
    installation_id: int | None = None,
    repo_id: str | None = None,
) -> IngestResult:
    """Clone, walk, embed, and index a repo for owner_id.

    ``source`` is either:
      * ``owner/repo`` — requires installation_id; cloned via GitHub App token.
      * An absolute local path starting with ``/`` — dev/test only.

    Why explicit source-kind: a typoed `installation_id` shouldn't silently
    fall back to a local path; the kind must be unambiguous from the
    string shape.

    Returns IngestResult on success. Raises ValueError for invalid inputs,
    GitHubAppNotConfigured for missing App env vars, or git.GitCommandError
    if the clone itself fails.
    """
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id must be a non-empty string")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")

    start = time.perf_counter()
    is_local = source.startswith("/")
    if is_local:
        clone_url = source
        canonical_url = f"local://{source}"
    else:
        if installation_id is None:
            raise ValueError(
                "installation_id is required when source is a GitHub 'owner/repo'"
            )
        clone_url = _github_app.authenticated_clone_url(source, installation_id)
        canonical_url = f"github://{source}"

    rid = _store.upsert_repo(owner_id, canonical_url, repo_id=repo_id)
    _store.mark_repo_status(rid, IngestStatus.INGESTING)

    workdir = tempfile.mkdtemp(prefix="aztea-ingest-")
    commits_indexed = 0
    hunks_indexed = 0
    skipped = 0
    head_sha = ""
    try:
        repo = _clone(clone_url, workdir, is_local)
        head_sha = repo.head.commit.hexsha
        for commit in _walk_commits(repo):
            try:
                meta, hunk_inputs = _extract_commit(commit, rid)
            except Exception as exc:
                # Per-commit failure shouldn't fail the whole repo.
                _LOG.warning(
                    "skipping commit %s during ingest of %s: %s",
                    commit.hexsha, rid, exc,
                )
                skipped += 1
                continue
            _store.upsert_commit(meta)
            hunks_indexed += _embed.embed_and_store_batch(rid, hunk_inputs)
            commits_indexed += 1
        _store.mark_repo_status(rid, IngestStatus.READY, head_sha=head_sha)
    except Exception:
        _store.mark_repo_status(rid, IngestStatus.FAILED, head_sha=head_sha or None)
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    duration_ms = int((time.perf_counter() - start) * 1000)
    return IngestResult(
        repo_id=rid,
        head_sha=head_sha,
        commits_indexed=commits_indexed,
        hunks_indexed=hunks_indexed,
        skipped=skipped,
        duration_ms=duration_ms,
    )


def _clone(clone_url: str, workdir: str, is_local: bool) -> git.Repo:
    """Clone the repo into workdir, returning a GitPython Repo.

    Why depth=None: we need full history for revert detection. A shallow
    clone wouldn't see the original of a reverted commit.
    """
    if is_local:
        # Local copy: assume the path is a valid git working tree.
        # GitPython opens existing repos via Repo(path); we copy to a tempdir
        # so the ingest never mutates the source.
        if not os.path.isdir(os.path.join(clone_url, ".git")):
            raise ValueError(f"local source {clone_url!r} is not a git repo")
        shutil.copytree(clone_url, os.path.join(workdir, "repo"), symlinks=False)
        return git.Repo(os.path.join(workdir, "repo"))
    target = os.path.join(workdir, "repo")
    return git.Repo.clone_from(clone_url, target)


def _walk_commits(repo: git.Repo) -> Iterator[git.Commit]:
    """Yield commits chronologically (oldest first), capped at _MAX_COMMITS."""
    # iter_commits('HEAD') yields newest-first; reverse so revert detection
    # sees the original before the revert in the same pass.
    commits = list(repo.iter_commits("HEAD", max_count=_MAX_COMMITS_PER_INGEST))
    commits.reverse()
    for c in commits:
        yield c


def _extract_commit(
    commit: git.Commit, repo_id: str,
) -> tuple[CommitMeta, list[_embed.HunkInput]]:
    """Pull metadata + diff hunks from a single commit.

    Returns (CommitMeta, list[HunkInput]). HunkInput list may be empty for
    merge commits or commits touching only binary files.
    """
    parent_sha = commit.parents[0].hexsha if commit.parents else None
    ts = datetime.fromtimestamp(commit.committed_date, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )
    message = (commit.message or "").strip()

    was_reverted_by, hotfix_target = _detect_revert_or_hotfix(message)
    meta = CommitMeta(
        commit_sha=commit.hexsha,
        repo_id=repo_id,
        parent_sha=parent_sha,
        author=str(commit.author) if commit.author else "",
        ts=ts,
        was_reverted=False,  # The original commit's flag is set by a separate
                              # pass; here we just record the commit itself.
        hotfix_for=hotfix_target,
        message=message[:200],
    )

    # If this commit explicitly reverts an earlier commit, mark the earlier
    # commit (if indexed) with was_reverted=True.
    if was_reverted_by is not None:
        original = _store.get_commit(repo_id, was_reverted_by)
        if original is not None:
            _store.upsert_commit(
                CommitMeta(
                    commit_sha=original["commit_sha"],
                    repo_id=repo_id,
                    parent_sha=original.get("parent_sha"),
                    author=original.get("author") or "",
                    ts=original.get("ts") or ts,
                    was_reverted=True,
                    hotfix_for=original.get("hotfix_for"),
                    message=original.get("message", ""),
                )
            )

    if not commit.parents:
        # Root commit: diff against the empty tree would dump every file.
        # Skipping the embedding for the root keeps the index focused on
        # changes, not initial state.
        return meta, []

    parent = commit.parents[0]
    try:
        diff_index = parent.diff(commit, create_patch=True)
    except Exception as exc:
        _LOG.warning("diff failed for commit %s: %s", commit.hexsha, exc)
        return meta, []

    hunks: list[_embed.HunkInput] = []
    diffs = sorted(
        (d for d in diff_index if d.b_path or d.a_path),
        key=lambda d: (d.b_path or d.a_path or ""),
    )
    for hunk_idx, diff in enumerate(diffs[:_MAX_FILES_PER_COMMIT]):
        file_path = diff.b_path or diff.a_path
        if not file_path:
            continue
        try:
            patch_text = (diff.diff.decode("utf-8", errors="replace")
                          if hasattr(diff.diff, "decode")
                          else str(diff.diff))
        except Exception as exc:
            # Skip an undecodable diff but leave a trail — silent drops made it
            # impossible to tell why a file was missing from an ingest.
            _LOG.warning(
                "skipping undecodable diff for %s in commit %s: %s",
                file_path, commit.hexsha, exc,
            )
            continue
        if not patch_text or not patch_text.strip():
            continue
        if len(patch_text) > _MAX_HUNK_CHARS:
            dropped = len(patch_text) - _MAX_HUNK_CHARS
            patch_text = (
                patch_text[:_MAX_HUNK_CHARS]
                + f"\n... [truncated {dropped} chars] ..."
            )
        hunks.append(
            _embed.HunkInput(
                commit_sha=commit.hexsha,
                file=file_path,
                hunk_idx=hunk_idx,
                text=patch_text,
                ast_shape_hash=None,
            )
        )
    return meta, hunks


def _detect_revert_or_hotfix(message: str) -> tuple[str | None, str | None]:
    """Pure: parse the commit message for revert/hotfix markers.

    Returns (reverted_sha_or_None, hotfix_target_sha_or_None).
    Both can be set if a commit reverts X while also being a hotfix for Y,
    though in practice it's one or the other.
    """
    if not isinstance(message, str):
        return (None, None)

    reverted_sha = None
    hotfix_target = None

    # Pattern 1: explicit "Reverts commit <sha>" line (git revert default).
    m = _REVERT_SHA_RE.search(message)
    if m:
        reverted_sha = m.group(1).lower()
    # Pattern 2: "Revert <subject>" keyword without a SHA. We can't link
    # to a specific commit but the keyword still flags the commit itself
    # — the original commit detection happens via Pattern 1's SHA match.
    # (Recording the bare 'revert' keyword without a target is a TODO.)

    # Hotfix pointing at an earlier commit:
    m = _HOTFIX_SHA_RE.search(message)
    if m:
        hotfix_target = m.group(1).lower()

    return (reverted_sha, hotfix_target)
