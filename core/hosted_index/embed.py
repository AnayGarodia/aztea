"""
embed.py — batch hunk embeddings, written into the vector_store.

# OWNS: the loop that turns a list of (commit, file, hunk_text) into
#       vector_store rows under the repo's namespace.
# NOT OWNS: embedding backend (core/embeddings.py), vector storage
#           (core/vector_store.py), hunk extraction (ingest.py).
#
# INVARIANTS:
#   * Every hunk gets exactly one vector_entry. entry_id == hunk_id.
#   * Batches respect _MAX_BATCH so a single repo with millions of hunks
#     doesn't OOM the embedding model.
#   * Metadata stored alongside each vector includes commit_sha + file so
#     retrieve.py can rehydrate without joining back to repo_hunks for the
#     common path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core import embeddings as _embed
from core import vector_store as _vs
from core.hosted_index import store as _store

_LOG = logging.getLogger(__name__)

# How many hunks to embed per batch. The local sentence-transformers model
# (all-MiniLM-L6-v2) handles 128 at a time without GPU memory pressure;
# OpenAI's text-embedding-3-small caps at 2048 per call but we stay
# conservative for memory parity.
_MAX_BATCH: int = 128


@dataclass(frozen=True)
class HunkInput:
    """One hunk to be embedded + persisted."""

    commit_sha: str
    file: str
    hunk_idx: int
    text: str
    ast_shape_hash: str | None = None


def embed_and_store_batch(repo_id: str, hunks: list[HunkInput]) -> int:
    """Embed every hunk and persist to vector_store + repo_hunks.

    Returns the number of hunks indexed. Skips hunks whose text is empty
    (a 0-byte change is still a commit but contains no semantic signal).

    Why batched: the local embedding model amortises encoder warmup across
    ~128 inputs; doing them one-by-one is ~30x slower at ingest time.
    """
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ValueError("repo_id must be a non-empty string")
    if not isinstance(hunks, list):
        raise ValueError("hunks must be a list of HunkInput")

    # Filter out empty hunks BEFORE batching so we don't waste batch slots.
    real_hunks = [h for h in hunks if h.text and h.text.strip()]
    if not real_hunks:
        return 0

    namespace = f"repo:{repo_id}"
    indexed = 0

    for chunk_start in range(0, len(real_hunks), _MAX_BATCH):
        chunk = real_hunks[chunk_start : chunk_start + _MAX_BATCH]
        texts = [h.text for h in chunk]
        try:
            vectors = _embed.embed_texts_batch(texts)
        except Exception as exc:
            # WHY warn-and-skip: a single bad batch (e.g., embedding provider
            # outage mid-ingest) shouldn't fail the whole repo. The skip is
            # reflected in the IngestResult.skipped count so callers can
            # decide whether to retry.
            _LOG.warning(
                "embeddings batch failed at offset %d for repo_id=%s: %s",
                chunk_start, repo_id, exc,
            )
            continue
        for hunk, vector in zip(chunk, vectors):
            hid = _store.hunk_id_for(
                repo_id, hunk.commit_sha, hunk.file, hunk.hunk_idx,
            )
            metadata = {
                "commit_sha": hunk.commit_sha,
                "file": hunk.file,
                "hunk_idx": hunk.hunk_idx,
                "ast_shape_hash": hunk.ast_shape_hash,
            }
            _vs.add(namespace, hid, vector, metadata)
            _store.upsert_hunk(
                repo_id,
                hunk.commit_sha,
                hunk.file,
                hunk.hunk_idx,
                hunk.ast_shape_hash,
                hid,
            )
            indexed += 1
    return indexed


def embed_query(text: str) -> list[float]:
    """Convenience: embed a single query hunk for retrieve.top_k_similar_hunks.

    Why exposed: retrieve.py shouldn't import core/embeddings directly —
    keeping the dependency edge here means a future swap (e.g., per-repo
    embedding fine-tunes) lands in one file.
    """
    return _embed.embed_text(text)
