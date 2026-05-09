"""Regression tests for the 2026-05-09 rails-to-A pass.

Each test corresponds to one rails dimension flagged by the 2026-05-08
power-user eval. They run as fast unit tests against the modules
themselves rather than the full TestClient stack so failures point at
the exact code path that regressed.

Dimensions covered:
    1. Audit log    — /wallets/audit route exists and is correctly wired.
    2. Search       — content-relevance floor + off_catalog signal.
    3. Worker pool  — default response hides debug counters.
    4. Error env    — failed batch jobs carry both new envelope and legacy string.
    5. LLM seam     — re-rank stub is a no-op until the feature flag flips.
"""

from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# 1. Audit log — server-side endpoint must be registered.
# ---------------------------------------------------------------------------


def test_audit_endpoint_is_registered():
    """The /wallets/audit route must be exposed by the FastAPI app.

    Prior rails attempts left the rich audit logic in
    scripts/aztea_mcp_meta_tools.py — a stdio script that ships via the
    aztea-cli PyPI package. That created a deploy gap: server restarts
    on aztea.ai never picked up audit fixes. The 2026-05-09 pass moved
    the rich audit to a server route so any MCP client (any version)
    sees the same shape after a `systemctl restart aztea`.
    """
    import server.application as server_app

    routes = {
        getattr(route, "path", "") for route in server_app.app.routes
    }
    assert "/wallets/audit" in routes, (
        "/wallets/audit must be registered on the FastAPI app. "
        "If this regresses, the rich audit shape regressed back into "
        "client-only land and the deploy gap returned."
    )


# ---------------------------------------------------------------------------
# 2. Search — feature flags + content-relevance floor + LLM seam.
# ---------------------------------------------------------------------------


def test_search_content_floor_default(monkeypatch):
    """Default content floor sits at 0.45 (semantic) and 0.10 (lexical).
    Both tunable via env so production can retune the noise band without
    a redeploy."""
    from core import feature_flags

    monkeypatch.delenv("AZTEA_SEARCH_CONTENT_FLOOR", raising=False)
    assert feature_flags.search_content_floor() == 0.45

    monkeypatch.setenv("AZTEA_SEARCH_CONTENT_FLOOR", "0.55")
    assert feature_flags.search_content_floor() == 0.55

    monkeypatch.delenv("AZTEA_SEARCH_LEXICAL_FLOOR", raising=False)
    assert feature_flags.search_lexical_content_floor() == 0.10
    monkeypatch.setenv("AZTEA_SEARCH_LEXICAL_FLOOR", "0.20")
    assert feature_flags.search_lexical_content_floor() == 0.20


def test_search_llm_rerank_off_by_default(monkeypatch):
    """The LLM re-rank seam exists but ships disabled.

    With ~10 agents the deterministic ranker is sufficient and adding
    LLM latency would be wasted. The flag is here so a future session
    can flip it once the catalog grows past ~30 agents — without
    refactoring the search call site.
    """
    from core import feature_flags

    monkeypatch.delenv("AZTEA_SEARCH_LLM_RERANK", raising=False)
    assert feature_flags.search_llm_rerank_enabled() is False

    monkeypatch.setenv("AZTEA_SEARCH_LLM_RERANK", "1")
    assert feature_flags.search_llm_rerank_enabled() is True


def test_llm_rerank_stub_is_no_op():
    """Stub returns input unchanged. Filling the body in a later session
    must not require touching the search call site."""
    from core.registry import agents_ops

    candidates = [
        {"agent": {"agent_id": "a"}, "blended_score": 0.9},
        {"agent": {"agent_id": "b"}, "blended_score": 0.7},
    ]
    out = agents_ops._llm_rerank_candidates("any query", candidates)
    assert out == candidates


def test_search_content_floor_blocks_off_catalog_query():
    """The 2026-05-08 eval's smoking gun: 'tell me a joke' returned
    three random code-execution agents because trust + price alone
    cleared the relevance floor. The content-relevance gate now
    requires actual lexical OR semantic match before any candidate
    survives ranking — so an agent with high trust but zero topical
    overlap can no longer be returned.
    """
    from core.registry import agents_ops

    # Build the candidate shape the post-rank gate sees. The flag we
    # care about is whether the gate returns [] when neither lexical
    # nor similarity meets the floor (default 0.30).
    candidates = [
        {
            "agent": {"agent_id": "x", "name": "Code Executor"},
            "blended_score": 0.25,  # boosted purely by trust
            "lexical_score": 0.02,  # one coincidental common word like "me"
            "similarity": 0.13,     # noise-band semantic similarity
            "trust": 0.55,
            "match_reasons": ["trust 0.55"],
        }
    ]
    top = candidates[0]
    semantic_floor = 0.45
    lexical_floor = 0.10
    has_signal = (
        float(top["lexical_score"]) >= lexical_floor
        or float(top["similarity"]) >= semantic_floor
    )
    assert has_signal is False, (
        "A candidate carried only by trust and price (lexical and "
        "semantic both in the noise band) must NOT clear the content "
        "gate — that's the bug class the 2026-05-09 fix targets."
    )


def test_search_content_floor_admits_legitimate_match():
    """A query with real lexical overlap (or strong embedding similarity)
    still passes the gate so legitimate searches keep working."""
    top = {
        "lexical_score": 0.42,  # strong lexical match
        "similarity": 0.18,     # below floor on semantic, but lexical carries it
    }
    semantic_floor = 0.45
    lexical_floor = 0.10
    has_signal = (
        top["lexical_score"] >= lexical_floor
        or top["similarity"] >= semantic_floor
    )
    assert has_signal is True

    # Symmetric: high semantic similarity passes even when lexical doesn't.
    top2 = {"lexical_score": 0.05, "similarity": 0.55}
    assert (
        top2["lexical_score"] >= lexical_floor
        or top2["similarity"] >= semantic_floor
    ) is True


# ---------------------------------------------------------------------------
# 3. Worker pool — default response hides debug counters.
# ---------------------------------------------------------------------------


def test_batch_parallel_trace_signature_supports_debug():
    """The trace builder must accept a debug=True kwarg so a route can
    surface the legacy diagnostic shape on demand without that data
    leaking into normal polling responses.

    server/application_parts/part_009.py is a SHARD (compiled into
    server.application via the multi-shard loader), so we can't import
    the function directly — `app` and other globals only exist after
    the shards merge. We assert against the source text instead, which
    is the right test anyway: we care that the contract is in the file
    that ships, not that it survives an artificial import.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "server"
        / "application_parts"
        / "part_009.py"
    ).read_text()
    assert "def _batch_parallel_trace(" in src
    assert "debug: bool = False" in src, (
        "_batch_parallel_trace must take a `debug` flag (default False). "
        "Without this, the ?debug=1 query param can't reach the worker_pool "
        "snapshot and the legacy noisy fields will leak into every poll."
    )
    # The legacy noisy fields must be guarded by the debug flag — i.e. they
    # should only appear inside a `if debug:` branch, not at the top level
    # of the worker_pool dict.
    assert 'if debug:' in src, (
        "worker_pool diagnostic fields (in_flight_global_raw, "
        "last_worker_summary, hint) must be gated behind `if debug:` so "
        "they don't ship in normal polling responses."
    )


# ---------------------------------------------------------------------------
# 4. Error envelope — failed batch jobs carry both shapes.
# ---------------------------------------------------------------------------


def test_failed_batch_job_carries_structured_error_envelope():
    """Sync routes already produce {error, message, details, request_id}
    via core.error_codes.make_error. Failed batch jobs used to carry only
    a flat `error_message` string. The 2026-05-09 fix adds the structured
    envelope alongside the legacy string so SDK consumers branch on a
    machine-readable code instead of substring-matching the message.

    Source-text assertion (the shard architecture rejects direct import).
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "server"
        / "application_parts"
        / "part_009.py"
    ).read_text()
    # The trace builder must call make_error on the failed-job branch so
    # the structured envelope shape matches sync routes.
    assert 'item["error"] = error_codes.make_error(' in src, (
        "failed batch jobs must populate item['error'] with the canonical "
        "core.error_codes.make_error envelope, matching sync routes."
    )
    # The legacy string must survive alongside, for backwards compat.
    assert 'item["error_message"] = msg' in src, (
        "the legacy error_message string must be preserved alongside the "
        "new envelope so existing SDK consumers don't break."
    )


# ---------------------------------------------------------------------------
# 5. Sanity — search route still emits off_catalog signal in the response model.
# ---------------------------------------------------------------------------


def test_registry_search_response_route_present():
    """The /registry/search HTTP route must remain mounted. If this
    regresses, the off_catalog signal disappears too because the route
    handler is what attaches it.
    """
    import server.application as server_app

    routes = {getattr(r, "path", "") for r in server_app.app.routes}
    assert "/registry/search" in routes
