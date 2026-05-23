"""
retrieve.py — query the hosted index for hunks similar to a candidate.

# OWNS: top_k_similar_hunks query API. Hydrates vector_store hits with
#       per-hunk metadata from repo_hunks.
# NOT OWNS: embedding (embed.py), storage (store.py + vector_store).
#
# INVARIANTS:
#   * Results are sorted by descending cosine score (vector_store guarantee).
#   * The query text is embedded with the SAME backend used at ingest time
#     (core/embeddings.py auto-selects), so cross-backend mismatches can't
#     silently return zero-relevance hits.
#   * Filter predicates run post-cosine; pre-filter (push-down into SQL)
#     is a v0.2 optimisation.
"""

from __future__ import annotations

import logging
from typing import Callable

from core import vector_store as _vs
from core.hosted_index import embed as _embed
from core.hosted_index.types import HunkMatch

_LOG = logging.getLogger(__name__)

_DEFAULT_K: int = 10
_MAX_K: int = 100  # Stays well under vector_store's _MAX_TOP_K of 200.


def top_k_similar_hunks(
    query_text: str,
    repo_id: str,
    k: int = _DEFAULT_K,
    exclude_commit_sha: str | None = None,
    file_filter: Callable[[str], bool] | None = None,
) -> list[HunkMatch]:
    """Return the top-k hunks in repo_id most similar to query_text.

    ``exclude_commit_sha`` skips hunks from the named commit — useful when
    the caller is asking "find similar past changes to this current diff"
    and doesn't want the diff itself to dominate the results.

    ``file_filter`` runs after retrieval against the hunk's file path.
    Use cases: scope similarity to the test directory, or skip vendored
    code that would otherwise crowd out real matches.

    Why query_text and not query_vector: callers (D16 Codebase Reviewer)
    have a hunk in hand, not a vector. Centralising the embed step here
    keeps the encoder choice consistent with ingest and avoids exposing
    the embedding module to every consumer.
    """
    if not isinstance(query_text, str) or not query_text.strip():
        raise ValueError("query_text must be a non-empty string")
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ValueError("repo_id must be a non-empty string")
    if k <= 0 or k > _MAX_K:
        raise ValueError(f"k must be in (0, {_MAX_K}], got {k}")

    namespace = f"repo:{repo_id}"
    vector = _embed.embed_query(query_text)

    # Why over-fetch: post-filter on metadata can drop several rows; without
    # over-fetching, file_filter on a popular path can return fewer than k.
    # The 3x multiplier is empirical; cheap on v0 brute-force scales.
    fetch_k = min(k * 3, _MAX_K)

    def _filter(meta: dict) -> bool:
        if exclude_commit_sha is not None and meta.get("commit_sha") == exclude_commit_sha:
            return False
        if file_filter is not None and not file_filter(meta.get("file", "")):
            return False
        return True

    hits = _vs.top_k(namespace, vector, k=fetch_k, filter_pred=_filter)

    out: list[HunkMatch] = []
    for hit in hits:
        if len(out) >= k:
            break
        out.append(
            HunkMatch(
                hunk_id=hit.entry_id,
                repo_id=repo_id,
                commit_sha=str(hit.metadata.get("commit_sha", "")),
                file=str(hit.metadata.get("file", "")),
                score=hit.score,
                ast_shape_hash=hit.metadata.get("ast_shape_hash"),
            )
        )
    return out
