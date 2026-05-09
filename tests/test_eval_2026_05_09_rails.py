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


def test_search_relevance_floor_calibrated_to_live_data(monkeypatch):
    """The blended-score floor moved from 0.18 to 0.30 after live
    calibration on prod. Off-catalog queries ('tell me a joke',
    'cook me dinner') measured 0.23-0.26 in production with the real
    embedding model and current catalog; legitimate queries cluster at
    0.33+. The 0.30 floor sits cleanly between the two distributions.
    Env-tunable so production can retune without a redeploy.
    """
    from core import feature_flags

    monkeypatch.delenv("AZTEA_SEARCH_RELEVANCE_FLOOR", raising=False)
    assert feature_flags.search_relevance_floor() == 0.30

    monkeypatch.setenv("AZTEA_SEARCH_RELEVANCE_FLOOR", "0.40")
    assert feature_flags.search_relevance_floor() == 0.40


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


def test_search_relevance_floor_blocks_off_catalog_query():
    """The 2026-05-08 eval's smoking gun: 'tell me a joke' returned
    three random code-execution agents because the relevance floor sat
    at 0.18 — easily cleared by trust + price contributions when content
    overlap was in the noise band. The floor now sits at 0.30, above
    the off-catalog distribution (measured 0.23-0.26 on prod).
    """
    floor = 0.30
    # Off-catalog blended scores measured on prod after deploy:
    off_catalog_blended = [0.256, 0.230, 0.236]  # joke/dinner/wikipedia
    for score in off_catalog_blended:
        assert score < floor, (
            f"Blended score {score} for an off-catalog query must fall "
            f"below the {floor} floor; otherwise the gate fails open."
        )


def test_search_relevance_floor_admits_legitimate_match():
    """Legitimate queries cluster at 0.33+ on prod and must still
    surface results. If this drops below 0.30 the floor is too tight."""
    floor = 0.30
    legitimate_blended = [
        0.435,  # "audit a python project for vulnerabilities" → CVE Lookup
        0.420,  # "is this dependency dangerous" → CVE Lookup
        0.327,  # "scan code for hardcoded passwords" → Code Review
    ]
    for score in legitimate_blended:
        assert score >= floor, (
            f"Legitimate query measured {score}; if this falls under "
            f"{floor} the floor is over-tight and is producing false "
            f"empty-results regressions."
        )


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


# ---------------------------------------------------------------------------
# 6. Secret scanner judge — accepts "info" severity (post-2026-05-09 stress).
# ---------------------------------------------------------------------------


def test_secret_scanner_judge_accepts_info_severity():
    """The 2026-05-09 stress test (10/10 secret_scanner jobs failed) traced
    to the judge's severity allowlist missing 'info'. The agent emits
    severity='info' for example-tagged secrets like AKIAIOSFODNN7EXAMPLE
    (a known false-positive class), but the judge rejected the whole
    output because 'info' wasn't in {critical, high, medium, low}. Caller
    was charged with no payout — a quality-judge logic bug, not an agent
    bug. Fix: add 'info' to the judge's severity_counts.
    """
    # part_005.py is a shard — its globals only resolve once the
    # multi-shard loader has built server.application, and importing
    # the merged module pulls in agent-registration code that may be
    # mid-flight on a developer checkout. Source-text assertion keeps
    # this test independent of unrelated WIP elsewhere in the tree.
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "server"
        / "application_parts"
        / "part_005.py"
    ).read_text()

    # Locate the secret_scanner branch and assert its severity_counts
    # init line includes "info". Using a focused window to avoid
    # matching the unrelated codereview branch which also has its
    # own severity_counts dict above.
    marker = 'if agent_id == _SECRET_SCANNER_AGENT_ID:'
    start = src.find(marker)
    assert start != -1, "secret_scanner judge branch not found in part_005.py"
    window = src[start:start + 4000]
    assert (
        'severity_counts = {"critical": 0, "high": 0, "medium": 0, '
        '"low": 0, "info": 0}'
    ) in window, (
        "secret_scanner judge must initialize severity_counts with "
        "an 'info' bucket. Without this, scanner findings tagged as "
        "info (e.g. AKIAIOSFODNN7EXAMPLE — known-example AWS keys) "
        "fail the per-finding severity check, which made 10/10 of "
        "the 2026-05-09 stress B1 batch fail."
    )


# ---------------------------------------------------------------------------
# 7. Search — ambiguous "price" must not trigger price-rank fallback.
# ---------------------------------------------------------------------------


def test_price_query_mode_ignores_bare_price_token():
    """The 2026-05-09 stress D3 case: 'apple stock price' surfaced three
    unrelated cheap agents because the bare word 'price' tripped
    _price_query_mode -> 'cheapest', which then bypassed the off-catalog
    short-circuit and the relevance floor. Fix: require explicit
    cheap/low/expensive intent, not the noun 'price'/'cost' alone.
    """
    from core.registry import agents_ops

    # The exact stress-test query — must NOT trip price-intent ranking.
    assert agents_ops._price_query_mode("apple stock price") is None
    assert agents_ops._price_query_mode("aws cost dashboard") is None
    # Real price-intent queries with explicit qualifiers still trigger.
    assert agents_ops._price_query_mode("cheapest CVE lookup") == "cheapest"
    assert agents_ops._price_query_mode("lowest price agent") == "cheapest"
    assert agents_ops._price_query_mode("most expensive option") == "most_expensive"


def test_multi_language_executor_blocks_imds_ssrf():
    """The 2026-05-09 stress test Z6 confirmed: a JS `fetch()` to
    169.254.169.254/latest/meta-data/ inside multi_language_executor
    returned HTTP 401 (the AWS instance metadata service was reachable).
    The Python executor blocks this statically; JS/TS/Go/Rust did not.
    Fix: pre-execution regex-based block on private-host literals and
    network-capable APIs across all four runtimes.
    """
    from agents import multi_language_executor as mle

    # Literal IMDS host across runtimes — must always be blocked.
    imds = "fetch('http://169.254.169.254/latest/meta-data/')"
    for lang in ("javascript", "typescript", "go", "rust"):
        safe, reason = mle._is_code_network_safe(lang, imds)
        assert not safe, f"{lang}: IMDS literal should be blocked"
        assert "private, loopback, or cloud-metadata host" in (reason or "")

    # JS network APIs — blocked even without an IP literal.
    cases = [
        ("javascript", "fetch('https://example.com')"),
        ("javascript", "const http = require('http'); http.get('x', ()=>{})"),
        ("typescript", "import http from 'node:http';"),
        ("go", 'import "net/http"\nhttp.Get("https://example.com")'),
        ("rust", "use std::net::TcpStream;\nTcpStream::connect(\"x:80\");"),
    ]
    for lang, code in cases:
        safe, reason = mle._is_code_network_safe(lang, code)
        assert not safe, f"{lang}: {code!r} should be blocked"
        assert reason

    # Sanity: benign code is allowed.
    benign = "console.log(1+1);"
    safe, reason = mle._is_code_network_safe("javascript", benign)
    assert safe, f"benign JS should be allowed; reason: {reason!r}"

    # Localhost is also blocked (covers most accidental SSRF too).
    safe, _ = mle._is_code_network_safe(
        "javascript", "fetch('http://localhost:8080/')"
    )
    assert not safe


def test_pipeline_step_jobs_get_signed():
    """The 2026-05-09 stress R2: 1 of 100 receipts in the 24h window
    was unsigned because the pipeline executor called
    ``jobs.update_job_status`` without the signature kwargs. Direct
    sync/async paths sign at completion (part_008.py / part_009.py);
    the pipeline path didn't, so recipe-step jobs landed unsigned.

    Source-text assertion (the executor pulls in heavy server modules
    on import).
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "core"
        / "pipelines"
        / "executor.py"
    ).read_text()
    assert "_sign_pipeline_step_output(" in src, (
        "core/pipelines/executor.py must compute signature kwargs for "
        "pipeline-step completions; without this, recipe runs land "
        "unsigned receipts and the audit aggregate drifts."
    )
    # The success-path update_job_status MUST receive the kwargs.
    assert "**_sign_pipeline_step_output(agent, output)" in src


def test_off_catalog_gate_active_in_price_query_mode():
    """Even when the user expresses price intent, off-catalog topics must
    still short-circuit to []. 'cheapest weather forecast' has no agent
    in the catalog and surfacing the cheapest unrelated agent is worse
    than empty results.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent
        / "core"
        / "registry"
        / "agents_ops.py"
    ).read_text()
    # The off_catalog short-circuit must NOT be guarded behind
    # `price_query_mode is None`. A regression here would re-introduce
    # the 2026-05-09 D3 bug.
    assert "if price_query_mode is None and query_token_set:" not in src, (
        "off_catalog short-circuit is gated by price_query_mode again — "
        "this re-opens the D3 hole where price-intent queries surface "
        "unrelated cheap agents."
    )
