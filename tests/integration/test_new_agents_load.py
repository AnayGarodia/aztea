"""
test_new_agents_load.py — performance guards (2 tests).

NOT a benchmark suite — these are tripwires that catch accidental
slowdowns. If they fail, profile the hot path; don't bump the thresholds
without understanding why the slowdown happened.

Both tests use mocked LLMs so they don't depend on real provider latency.
"""

from __future__ import annotations

import os
import time
from statistics import median

import pytest

os.environ.setdefault("API_KEY", "test-master-key")
os.environ.setdefault("SERVER_BASE_URL", "http://localhost:8000")

from tests.agent_helpers import (
    _build_fixture_repo,
    _stub_llm_factory,
    patch_llm_everywhere,
)


# ---------------------------------------------------------------------------
# 1. D16 latency guard — p50 < 2 seconds over 10 hunks
# ---------------------------------------------------------------------------


def test_d16_p50_latency_under_2s_with_mocked_llm(monkeypatch, tmp_path):
    """Median wall-clock for D16 reviewing 10 hunks (mocked LLM) must
    stay under 2 seconds.

    Why: real-time PR review is the load-bearing UX. If we regress past
    2s p50 with mocked LLMs, the actual provider call won't help us hit
    the user-visible budget either.
    """
    from core import hosted_index as hi
    from agents import codebase_reviewer

    # Ingest a fixture repo so D16 has something to query against.
    repo_path, _ = _build_fixture_repo(tmp_path, "bug_revert_fix")
    result = hi.ingest_repo(owner_id="perf-test", source=str(repo_path))

    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"verdict":"ok","rationale":"r","summary":"s","confidence":"low"}',
    ))
    hunks = [
        {"file": f"file_{i}.py", "text": f"def f{i}(x):\n    return x + {i}"}
        for i in range(10)
    ]

    samples_ms: list[float] = []
    for _ in range(5):
        t0 = time.perf_counter()
        out = codebase_reviewer.run({
            "repo_id": result.repo_id, "hunks": hunks,
        })
        elapsed_ms = (time.perf_counter() - t0) * 1000
        samples_ms.append(elapsed_ms)
        # Sanity — the call must succeed each iteration.
        assert isinstance(out, dict)

    p50 = median(samples_ms)
    assert p50 < 2000, (
        f"D16 p50 latency regression: {p50:.0f}ms (threshold 2000ms). "
        f"samples={[f'{s:.0f}ms' for s in samples_ms]}"
    )

    hi.delete_repo(result.repo_id)


# ---------------------------------------------------------------------------
# 2. C11 throughput guard — sustained ≥ 50 attestations/second
# ---------------------------------------------------------------------------


def test_c11_throughput_at_least_50_per_second(monkeypatch, tmp_path):
    """Mocked-LLM C11 must sustain ≥ 50 attestations per second.

    Why: signing is the load-bearing path for the compliance product;
    if Ed25519 sign becomes a bottleneck (e.g. someone swaps the lib),
    this catches it without needing a separate benchmark suite.
    """
    monkeypatch.setenv("AZTEA_COMPLIANCE_SIGNING_KEY_PATH",
                        str(tmp_path / "compliance_key.pem"))
    patch_llm_everywhere(monkeypatch, _stub_llm_factory(
        '{"summary":"s","rationale":"r"}',
    ))

    from agents import compliance_attestor as ca

    # Warm up — generate the signing key on the first call (it's lazy).
    _ = ca.run({
        "control": "SOC2_CC6_1", "pr_ref": "warm/up#0",
        "check_results": [
            {"check_id": "auth_required_on_protected_routes", "passed": True},
            {"check_id": "secrets_not_committed_to_repo", "passed": True},
            {"check_id": "encryption_in_transit_for_external_traffic",
              "passed": True},
            {"check_id": "principle_of_least_privilege_in_iam_diffs",
              "passed": True},
        ],
    })

    # Now measure.
    checks = [
        {"check_id": "auth_required_on_protected_routes", "passed": True},
        {"check_id": "secrets_not_committed_to_repo", "passed": True},
        {"check_id": "encryption_in_transit_for_external_traffic", "passed": True},
        {"check_id": "principle_of_least_privilege_in_iam_diffs", "passed": True},
    ]
    N = 50
    t0 = time.perf_counter()
    for i in range(N):
        out = ca.run({
            "control": "SOC2_CC6_1", "pr_ref": f"perf/test#{i}",
            "check_results": checks,
        })
        assert out.get("status") == "attested"
    elapsed = time.perf_counter() - t0
    rate = N / elapsed

    assert rate >= 50, (
        f"C11 throughput regression: {rate:.1f}/sec (threshold 50/sec). "
        f"Took {elapsed:.2f}s for {N} attestations."
    )
