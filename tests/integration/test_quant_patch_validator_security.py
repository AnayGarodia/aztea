"""Adversarial / hostile-input tests for `quant_patch_validator`.

# OWNS: tests that prove the agent CANNOT be turned into a sandbox-escape
#        vector AND that document explicitly the LIMITATIONS of in-process
#        execution (which is what v1 does — true containment is the
#        `live_sandbox` v0.2 follow-up).
# NOT OWNS: agent behaviour on valid input (see lifecycle / corpus).
# DECISIONS:
#   - We mark this entire file `pytest.mark.security` so it runs on
#     every commit (security is non-negotiable).
#   - The hostile candidates run IN-PROCESS. We assert containment for
#     the cases we DO control (recursion depth, memory bombs, infinite
#     loops via budget enforcement) and we document explicitly where
#     v1 does NOT contain (subprocess spawn, network, filesystem write).
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from agents.quant_patch_validator import run as validator_run


pytestmark = pytest.mark.security


_TRIVIAL_REF = "def f(x): return x * 2\n"


# ---------------------------------------------------------------------------
# Containment — agent budget enforces ceiling on hostile candidates
# ---------------------------------------------------------------------------


def test_infinite_loop_candidate_caught_via_per_call_timeout():
    """Infinite-loop candidate is contained by harness.call_both's per-call
    timeout (2.5s) and surfaces as a TimeoutError-classified regression.
    Total elapsed must not exceed budget + one per-call timeout + slack."""
    cand = "def f(x):\n    while True: pass\n    return x\n"
    started = time.time()
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 4,
        }
    )
    elapsed = time.time() - started
    # 4s budget + 2.5s per-call timeout + 2s slack = 8.5s ceiling.
    # We've measured ~6-9s empirically on M-class macOS.
    assert elapsed < 12.0, f"agent took {elapsed:.1f}s for an infinite-loop candidate"
    # The verdict must be regressions_found or contract_broken — never
    # 'equivalent' (that would mean we silently swallowed the hang).
    assert out["verdict"] in ("regressions_found", "contract_broken"), out


def test_recursive_explosion_candidate_caught_as_failure():
    """Infinite recursion → RecursionError on every input → contract_broken
    (consistent failure mode is a contract change, not value drift)."""
    cand = "def f(x): return f(x)\n"
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 4,
        }
    )
    # The agent classifies "cand always raises, ref never does" as
    # contract_broken (≥5 supporting members of exception_mismatch).
    assert out["verdict"] in ("contract_broken", "regressions_found"), out


def test_always_raising_candidate_classified_as_failure():
    """Candidate that always raises → contract_broken."""
    cand = "def f(x):\n    raise ValueError('always bad')\n"
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 3,
        }
    )
    assert out["verdict"] in ("contract_broken", "regressions_found"), out


# ---------------------------------------------------------------------------
# Self-import block (defence-in-depth)
# ---------------------------------------------------------------------------


def test_self_import_in_candidate_blocked():
    cand = "import agents.quant_patch_validator\ndef f(x): return x\n"
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.self_reference_blocked"


def test_self_import_in_reference_blocked():
    ref = "from agents.quant_patch_validator import run\ndef f(x): return x\n"
    out = validator_run(
        {
            "reference_code": ref,
            "candidate_code": "def f(x): return x\n",
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert out["error"]["code"] == "quant_patch_validator.self_reference_blocked"


def test_self_import_via_string_construction_not_blocked():
    """Document the known limitation: dynamic __import__ bypasses our static
    check. True containment requires `live_sandbox` (v0.2)."""
    cand = (
        "def f(x):\n"
        "    # Bypass: __import__ via string concat\n"
        "    mod = __import__('agents.' + 'quant_patch_validator')\n"
        "    return x * 2\n"
    )
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    # This is the documented limitation: dynamic import is NOT blocked.
    # The candidate runs and either succeeds or hits a recursion if it
    # invokes run() itself. We assert the call returns a dict, not that
    # it's blocked.
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# Documented limitations — v1 is NOT a sandbox
# ---------------------------------------------------------------------------


def test_filesystem_write_to_temp_runs_in_process(tmp_path):
    """v1 doesn't isolate filesystem. Test documents that and asserts cleanup."""
    target = tmp_path / "qpv_security_test_marker.txt"
    cand = (
        f"def f(x):\n"
        f"    with open({str(target)!r}, 'w') as f:\n"
        f"        f.write('written-by-cand')\n"
        f"    return x * 2\n"
    )
    out = validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
        }
    )
    assert isinstance(out, dict)
    # The candidate DID write to the filesystem (v1 limitation).
    # If we ever start sandboxing in-agent, flip this assertion.
    assert target.exists(), (
        "v1 documented limitation: filesystem writes are NOT isolated. "
        "If this test fails, the agent gained sandboxing — update runbook."
    )


def test_sys_path_modification_not_isolated():
    """Document that sys.path modifications by the candidate persist."""
    sentinel = "/__qpv_security_test_sentinel__"
    cand = (
        f"import sys\n"
        f"def f(x):\n"
        f"    if {sentinel!r} not in sys.path:\n"
        f"        sys.path.append({sentinel!r})\n"
        f"    return x\n"
    )
    try:
        validator_run(
            {
                "reference_code": _TRIVIAL_REF,
                "candidate_code": cand,
                "fuzz_budget": "quick",
                "fuzz_seconds": 2,
            }
        )
        # v1 limitation: sys.path WAS mutated by the candidate.
        assert sentinel in sys.path, (
            "v1 documented limitation: sys.path is NOT isolated. "
            "Live in-process; sandbox in v0.2 will isolate this."
        )
    finally:
        # Clean up so we don't poison subsequent tests.
        while sentinel in sys.path:
            sys.path.remove(sentinel)


# ---------------------------------------------------------------------------
# examples_sensitive guarantee (the privacy invariant)
# ---------------------------------------------------------------------------


def test_spec_carries_examples_sensitive_flag():
    """Sanity: the spec's examples_sensitive flag is True so the work-example
    recorder never replays caller code as public examples."""
    from server.builtin_agents.constants import QUANT_PATCH_VALIDATOR_AGENT_ID
    from server.builtin_agents.specs import builtin_agent_specs

    spec = next(
        s for s in builtin_agent_specs() if s["agent_id"] == QUANT_PATCH_VALIDATOR_AGENT_ID
    )
    assert spec.get("examples_sensitive") is True, (
        "PRIVACY INVARIANT: examples_sensitive MUST be True to block "
        "_record_public_work_example from replaying caller code."
    )


# ---------------------------------------------------------------------------
# Workspace artifact never leaks raw candidate source
# ---------------------------------------------------------------------------


def test_workspace_report_artifact_does_not_embed_candidate_source(tmp_path, monkeypatch):
    """When `_workspace_id` is provided, the report we write must not
    include the raw candidate_code in the artifact (signature is fine —
    just the function shape, not the full body)."""
    captured = []

    def fake_write_artifact(ws_id, path, body, content_type, **kwargs):
        captured.append({"path": path, "body": body})

    # Force a non-error path: monkeypatch the workspace module the agent imports.
    import core.workspaces as ws_mod
    monkeypatch.setattr(ws_mod, "write_artifact", fake_write_artifact)

    secret = "SECRET_SAUCE_PROPRIETARY_ALPHA_xxxxxxxxxxx"
    cand = (
        f"def f(x):\n"
        f"    # {secret}\n"
        f"    return x * 2\n"
    )
    validator_run(
        {
            "reference_code": _TRIVIAL_REF,
            "candidate_code": cand,
            "fuzz_budget": "quick",
            "fuzz_seconds": 2,
            "_workspace_id": "ws_test123",
        }
    )
    assert captured, "expected at least one workspace artifact write"
    for entry in captured:
        body_str = entry["body"].decode("utf-8") if isinstance(entry["body"], bytes) else str(entry["body"])
        assert secret not in body_str, (
            f"PRIVACY VIOLATION: candidate source leaked to workspace artifact: {entry['path']}"
        )
