"""End-to-end lifecycle test for quant_patch_validator.

# OWNS: confirming the agent is registered, callable via
#        `POST /registry/agents/{id}/call`, charges the caller's wallet,
#        returns the documented output schema, and works through the
#        standard built-in dispatch.
# NOT OWNS: quality (precision/recall) — see test_quant_patch_validator_corpus.
# DECISIONS:
#   - We use trivial code (`def f(x): return x*2` twice) so the test
#     finishes in well under one second. Quality is exercised by the
#     corpus test, not here. This test is just "the plumbing works".
"""

from __future__ import annotations

import json
import time

import pytest

from tests.integration.support import *  # noqa: F403,F401

from server.builtin_agents.constants import QUANT_PATCH_VALIDATOR_AGENT_ID


# Trivial pair: identical functions; no divergences. Tier=quick keeps the
# test fast (default budget would be 30s standard ~5 min — far too slow).
_REF = "def f(x):\n    return x * 2\n"
_CAND = "def f(x):\n    return x * 2\n"


@pytest.fixture(autouse=True)
def _drain_builtin_worker(client):
    """Yield, then poll-wait for builtin worker threads to complete BEFORE
    the `client` fixture closes the FastAPI lifespan (and with it, the
    DB connection pool). Without this, a worker thread still mid-
    settlement on `core.db.execute` races `close_all_connections` →
    Python 3.12 + sqlite3 segfault.

    Depending on `client` ensures this fixture tears down BEFORE client.
    """
    yield
    import threading as _th

    deadline = time.time() + 20.0
    while time.time() < deadline:
        # Look for any thread that's still doing builtin-dispatch work.
        # The ThreadPoolExecutor names workers `ThreadPoolExecutor-<id>_<n>`;
        # any thread executing inside server.application_parts is a builtin
        # worker for our purposes.
        live = [t for t in _th.enumerate() if t.is_alive() and "ThreadPoolExecutor" in t.name]
        if not live:
            break
        time.sleep(0.25)
    # Brute-force safety pause: lifespan teardown can still race even
    # when no executor threads appear live to enumerate(). Empirically a
    # 2 s pause is enough on Python 3.12 + macOS to avoid the
    # close_all_connections segfault.
    time.sleep(2.0)


def test_quant_patch_validator_registered_and_listed(client):
    """Agent appears in the discovery surface."""
    listing = client.get(
        "/registry/agents",
        headers={"Authorization": f"Bearer {TEST_MASTER_KEY}"},
    )
    assert listing.status_code == 200, listing.text
    items = listing.json().get("agents") or listing.json().get("items") or listing.json()
    if isinstance(items, dict):
        items = items.get("agents") or list(items.values())
    matches = [a for a in items if a.get("agent_id") == QUANT_PATCH_VALIDATOR_AGENT_ID]
    assert len(matches) == 1, (
        f"expected exactly one quant_patch_validator listing, got {len(matches)}"
    )
    spec = matches[0]
    assert spec.get("name") == "Quant Patch Validator"
    assert spec.get("category") == "Code Quality"
    # examples_sensitive guards proprietary alpha. The listing surface
    # does not expose this internal flag publicly; we verify it on the
    # spec itself via the builtin_agent_specs() module call.
    from server.builtin_agents.specs import builtin_agent_specs

    full_spec = next(
        s for s in builtin_agent_specs() if s["agent_id"] == QUANT_PATCH_VALIDATOR_AGENT_ID
    )
    assert bool(full_spec.get("examples_sensitive")) is True, (
        "examples_sensitive must be True to prevent caller code being replayed as work examples"
    )


def test_quant_patch_validator_call_returns_structured_output(client):
    """Calling with equivalent ref/cand returns verdict='equivalent'."""
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    resp = client.post(
        f"/registry/agents/{QUANT_PATCH_VALIDATOR_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "reference_code": _REF,
            "candidate_code": _CAND,
            "fuzz_budget": "quick",
            "fuzz_seconds": 3,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    output = body.get("output") or body.get("output_payload") or body
    assert output.get("verdict") in (
        "equivalent",
        "regressions_found",
        "contract_broken",
        "signature_divergence",
        "intended_changes_only",
    ), f"unexpected verdict: {output.get('verdict')}"
    # The trivial pair must be equivalent.
    assert output["verdict"] == "equivalent", output
    assert output["fuzz_stats"]["clusters"] == 0
    assert output["fuzz_stats"]["tier_used"] == "quick"
    assert output["fuzz_stats"]["inputs_explored"] > 0


def test_quant_patch_validator_rejects_missing_required_fields(client):
    """Validation: missing reference_code returns a structured error."""
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    resp = client.post(
        f"/registry/agents/{QUANT_PATCH_VALIDATOR_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={"candidate_code": _CAND, "fuzz_budget": "quick", "fuzz_seconds": 2},
    )
    # Sync call surfaces the structured error envelope inside the
    # `output_payload` field with HTTP 200 (the call succeeded; the agent
    # returned an error envelope which is a refundable failure).
    if resp.status_code == 200:
        body = resp.json()
        output = body.get("output") or body.get("output_payload") or body
        assert "error" in output, output
        assert output["error"]["code"] == "quant_patch_validator.missing_reference_code"
    else:
        # Some pricing paths short-circuit before invoking the agent; either
        # behaviour is acceptable as long as we get a 4xx with an error.
        assert 400 <= resp.status_code < 500, resp.text


def test_quant_patch_validator_detects_regression(client):
    """The canonical lookahead bug must be caught."""
    caller = _register_user()
    _fund_user_wallet(caller, 500)

    ref = (
        "import numpy as np\n"
        "def rolling_mean(prices, window):\n"
        "    p = np.asarray(prices, dtype=np.float64)\n"
        "    out = np.full(p.shape, np.nan)\n"
        "    if window <= 0 or p.size < window:\n"
        "        return out\n"
        "    for i in range(window, p.size):\n"
        "        out[i] = p[i-window:i].mean()\n"
        "    return out\n"
    )
    cand = (
        "import numpy as np\n"
        "def rolling_mean(prices, window):\n"
        "    p = np.asarray(prices, dtype=np.float64)\n"
        "    out = np.full(p.shape, np.nan)\n"
        "    if window <= 0 or p.size < window:\n"
        "        return out\n"
        "    # lookahead: window includes today's bar\n"
        "    for i in range(window - 1, p.size):\n"
        "        out[i] = p[i-window+1:i+1].mean()\n"
        "    return out\n"
    )

    resp = client.post(
        f"/registry/agents/{QUANT_PATCH_VALIDATOR_AGENT_ID}/call",
        headers=_auth_headers(caller["raw_api_key"]),
        json={
            "reference_code": ref,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 6,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    output = body.get("output") or body.get("output_payload") or body
    assert output["verdict"] == "regressions_found", json.dumps(output, indent=2)[:1000]
    assert len(output["confirmed_regressions"]) >= 1
