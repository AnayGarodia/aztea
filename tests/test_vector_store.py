"""Tests for core/vector_store.py — namespaced vector store with top-K cosine."""

from __future__ import annotations

import numpy as np
import pytest

from core import vector_store as vs
from core.embeddings import EMBEDDING_DIM


def _make_vec(seed: int) -> list[float]:
    """Deterministic random vector — same seed always returns the same vector."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal(EMBEDDING_DIM).astype(np.float32).tolist()


@pytest.fixture(autouse=True)
def _clean_namespaces():
    """Clear test namespaces before and after each test for isolation."""
    namespaces_used = [
        "test_basic",
        "test_recall",
        "test_filter",
        "test_batch",
        "test_validation",
        "test_replace",
    ]
    for ns in namespaces_used:
        vs.delete_namespace(ns)
    yield
    for ns in namespaces_used:
        vs.delete_namespace(ns)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


def test_add_and_get_roundtrip():
    vs.add("test_basic", "alpha", _make_vec(1), {"label": "first"})
    hit = vs.get("test_basic", "alpha")
    assert hit is not None
    assert hit.namespace == "test_basic"
    assert hit.entry_id == "alpha"
    assert hit.metadata == {"label": "first"}


def test_get_missing_returns_none():
    assert vs.get("test_basic", "does_not_exist") is None


def test_count_reflects_inserts_and_deletes():
    assert vs.count("test_basic") == 0
    vs.add("test_basic", "a", _make_vec(1), {})
    vs.add("test_basic", "b", _make_vec(2), {})
    assert vs.count("test_basic") == 2
    assert vs.delete("test_basic", "a") is True
    assert vs.count("test_basic") == 1
    assert vs.delete("test_basic", "a") is False  # Already gone.


def test_delete_namespace_clears_all():
    for i in range(5):
        vs.add("test_basic", f"e_{i}", _make_vec(i), {})
    deleted = vs.delete_namespace("test_basic")
    assert deleted == 5
    assert vs.count("test_basic") == 0


def test_add_replaces_existing_entry():
    vs.add("test_replace", "id1", _make_vec(1), {"version": 1})
    vs.add("test_replace", "id1", _make_vec(2), {"version": 2})
    assert vs.count("test_replace") == 1
    hit = vs.get("test_replace", "id1")
    assert hit is not None
    assert hit.metadata["version"] == 2


# ---------------------------------------------------------------------------
# top_k semantics
# ---------------------------------------------------------------------------


def test_self_query_returns_score_one():
    target_vec = _make_vec(100)
    vs.add("test_basic", "target", target_vec, {"idx": "target"})
    for i in range(5):
        vs.add("test_basic", f"other_{i}", _make_vec(i), {"idx": i})
    hits = vs.top_k("test_basic", target_vec, k=1)
    assert len(hits) == 1
    assert hits[0].entry_id == "target"
    assert hits[0].score == pytest.approx(1.0, abs=1e-5)


def test_top_k_returns_k_results_in_score_order():
    for i in range(20):
        vs.add("test_basic", f"e_{i}", _make_vec(i), {"idx": i})
    hits = vs.top_k("test_basic", _make_vec(0), k=5)
    assert len(hits) == 5
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), "results must be sorted by descending score"


def test_top_k_empty_namespace_returns_empty():
    assert vs.top_k("test_basic", _make_vec(1), k=10) == []


def test_filter_pred_excludes_metadata_match():
    for i in range(10):
        vs.add("test_filter", f"e_{i}", _make_vec(i), {"kind": "odd" if i % 2 else "even"})
    hits = vs.top_k(
        "test_filter",
        _make_vec(0),
        k=20,
        filter_pred=lambda m: m.get("kind") == "even",
    )
    assert all(h.metadata["kind"] == "even" for h in hits)
    assert len(hits) == 5


def test_recall_against_brute_force_at_1000_vectors():
    """The plan-level acceptance test: recall@10 ≥ 0.95 vs. brute-force cosine."""
    rng = np.random.default_rng(2024)
    vectors = rng.standard_normal((1000, EMBEDDING_DIM)).astype(np.float32)
    for i, v in enumerate(vectors):
        vs.add("test_recall", f"v_{i}", v.tolist(), {"idx": i})

    query = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    hits = vs.top_k("test_recall", query.tolist(), k=10)
    got_ids = {h.entry_id for h in hits}

    # Brute-force reference: normalise both sides, dot-product, top-10.
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalised_matrix = vectors / norms
    normalised_query = query / max(float(np.linalg.norm(query)), 1.0)
    reference_scores = normalised_matrix @ normalised_query
    top_10_indices = np.argsort(-reference_scores)[:10]
    expected_ids = {f"v_{int(i)}" for i in top_10_indices}

    recall = len(got_ids & expected_ids) / len(expected_ids)
    assert recall >= 0.95, (
        f"recall@10 = {recall:.3f} < 0.95. "
        f"got={got_ids}, expected={expected_ids}"
    )


# ---------------------------------------------------------------------------
# Batch insert
# ---------------------------------------------------------------------------


def test_add_batch_inserts_all():
    entries = [
        (f"b_{i}", _make_vec(i), {"idx": i})
        for i in range(50)
    ]
    inserted = vs.add_batch("test_batch", entries)
    assert inserted == 50
    assert vs.count("test_batch") == 50


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_wrong_dimension_raises():
    with pytest.raises(ValueError, match=str(EMBEDDING_DIM)):
        vs.add("test_validation", "x", [0.1, 0.2, 0.3], {})


def test_non_finite_vector_raises():
    vec = _make_vec(1)
    vec[0] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        vs.add("test_validation", "x", vec, {})


def test_empty_namespace_raises():
    with pytest.raises(ValueError, match="namespace"):
        vs.add("", "x", _make_vec(1), {})


def test_empty_entry_id_raises():
    with pytest.raises(ValueError, match="entry_id"):
        vs.add("test_validation", "", _make_vec(1), {})


def test_non_serialisable_metadata_raises():
    with pytest.raises(ValueError, match="JSON"):
        vs.add("test_validation", "x", _make_vec(1), {"obj": object()})


def test_k_out_of_range_raises():
    with pytest.raises(ValueError, match="k"):
        vs.top_k("test_basic", _make_vec(1), k=0)
    with pytest.raises(ValueError, match="k"):
        vs.top_k("test_basic", _make_vec(1), k=10_000)


def test_oversized_namespace_raises():
    with pytest.raises(ValueError, match="namespace"):
        vs.add("x" * 200, "id", _make_vec(1), {})
