"""Regression tests for the 2026-05-18 cleanup sprint (P0/P1 fixes).

Each test pins a behaviour that previously regressed or was incomplete.
Tests are deliberately small and code-source-anchored where the full
runtime is too heavy to spin up.

NOTE: server/application_parts/part_*.py are shards that share one
namespace assembled by server/application.py — they CANNOT be imported
standalone. Tests import the assembled module via ``server.application``.
"""

from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# C3 — hire_async wall-clock budget is separate from the sync 8s budget.
# Previously regressed: the async worker dispatched through the sync budget
# table, so hire_async inherited the 8s sync cap and refunded long jobs.
# ---------------------------------------------------------------------------


def test_c3_async_default_budget_is_at_least_300s():
    """The async tier must be measured in minutes, not seconds."""
    from server import application as server

    assert server._AGENT_WALL_BUDGET_ASYNC_DEFAULT_SECONDS >= 300.0, (
        f"async default budget is "
        f"{server._AGENT_WALL_BUDGET_ASYNC_DEFAULT_SECONDS}s — must be "
        "at least 300s per the work order (suggested minimum)"
    )


def test_c3_async_budget_distinct_from_sync_budget():
    """The two budget tables must not share the same default constant."""
    from server import application as server

    assert (
        server._AGENT_WALL_BUDGET_ASYNC_DEFAULT_SECONDS
        > server._AGENT_WALL_BUDGET_DEFAULT_SECONDS
    ), "async budget must exceed sync budget"
    assert server._AGENT_WALL_BUDGET_ASYNC_OVERRIDES is not (
        server._AGENT_WALL_BUDGET_OVERRIDES
    ), "override tables must be separate dicts"


def test_c3_resolve_wall_budget_picks_async_table_for_async_mode():
    """Helper must return the async table value when execution_mode='async'."""
    from server import application as server

    fake_agent = "00000000-0000-0000-0000-deadbeef0001"
    sync = server._resolve_wall_budget(fake_agent, "sync")
    asyn = server._resolve_wall_budget(fake_agent, "async")
    assert sync == server._AGENT_WALL_BUDGET_DEFAULT_SECONDS
    assert asyn == server._AGENT_WALL_BUDGET_ASYNC_DEFAULT_SECONDS
    assert asyn > sync


def test_c3_resolve_wall_budget_honors_overrides_per_mode():
    """Per-agent overrides must apply within their own mode's table."""
    from server import application as server

    overridden = next(iter(server._AGENT_WALL_BUDGET_OVERRIDES))
    sync = server._resolve_wall_budget(overridden, "sync")
    asyn = server._resolve_wall_budget(overridden, "async")
    assert sync == server._AGENT_WALL_BUDGET_OVERRIDES[overridden]
    # If the agent has an async override, it should be honored; otherwise
    # it falls back to the async default — either way, never less than sync.
    assert asyn >= sync


def test_c3_async_worker_passes_execution_mode_async():
    """The async worker loop must thread execution_mode='async' through."""
    src = Path("server/application_parts/part_004.py").read_text()
    # The call site inside the worker loop should pass
    # execution_mode="async". Match across newlines.
    pattern = re.compile(
        r"_execute_builtin_agent\(.*?execution_mode\s*=\s*[\"']async[\"']",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "async worker loop must pass execution_mode='async' to "
        "_execute_builtin_agent — otherwise the sync budget bleeds in"
    )


def test_c3_hire_async_tool_description_documents_async_budget():
    """The hire_async tool description must state the actual async budget."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # Locate the aztea_hire_async tool block.
    idx = src.find('"name": "aztea_hire_async"')
    assert idx >= 0, "aztea_hire_async tool block must exist"
    block = src[idx : idx + 4000]
    assert "minutes" in block, (
        "hire_async description must explicitly mention the budget in minutes"
    )
    assert "600" in block or "10 minutes" in block, (
        "hire_async description must reference the async default budget"
    )


# ---------------------------------------------------------------------------
# A1 / A2 — cold-start agents (zero traffic) no longer report
# success_rate=1.0. Previously misleading: broken endpoints with
# success_rate=1.0 / 0 calls outranked battle-tested agents.
# ---------------------------------------------------------------------------


def test_a1_a2_cold_start_success_rate_is_none_not_one():
    """A zero-traffic agent must surface success_rate=None, not 1.0."""
    from core.registry import core_schema

    row = {
        "total_calls": 0,
        "successful_calls": 0,
        "healthcheck_url": None,
        "output_verifier_url": None,
        "verified": 0,
        "internal_only": 0,
        "review_note": None,
        "reviewed_at": None,
        "reviewed_by": None,
        "trust_decay_multiplier": 1.0,
        "last_decay_at": "1970-01-01",
        "model_provider": None,
        "model_id": None,
        "pii_safe": 0,
        "outputs_not_stored": 0,
        "audit_logged": 0,
        "region_locked": None,
    }
    out = core_schema._row_to_dict(row)
    assert out["success_rate"] is None, (
        "cold-start agents must surface success_rate=None — 1.0 with zero "
        "calls misled buyers comparing trust signals"
    )
    assert out["has_call_history"] is False


def test_a1_a2_warm_agent_success_rate_still_computed():
    """Warm agents (total_calls>0) must still report a real success_rate."""
    from core.registry import core_schema

    row = {
        "total_calls": 10,
        "successful_calls": 9,
        "healthcheck_url": None,
        "output_verifier_url": None,
        "verified": 0,
        "internal_only": 0,
        "review_note": None,
        "reviewed_at": None,
        "reviewed_by": None,
        "trust_decay_multiplier": 1.0,
        "last_decay_at": "1970-01-01",
        "model_provider": None,
        "model_id": None,
        "pii_safe": 0,
        "outputs_not_stored": 0,
        "audit_logged": 0,
        "region_locked": None,
    }
    out = core_schema._row_to_dict(row)
    assert out["success_rate"] == 0.9
    assert out["has_call_history"] is True


# ---------------------------------------------------------------------------
# A3 / A4 / A5 — worker-image runtime deps are baked in. Verified at the
# source — Dockerfile is the source of truth for the prod image.
# ---------------------------------------------------------------------------


def test_a3_a4_a5_worker_image_bakes_required_deps():
    """Dockerfile must include coverage/pytest, checkov, hadolint, node."""
    src = Path("Dockerfile").read_text()
    for dep in ("coverage", "pytest", "checkov", "hadolint", "nodejs"):
        assert dep in src, (
            f"Dockerfile must bake {dep!r} — agents that shell out to it "
            "previously returned `tool_unavailable` in prod"
        )


# ---------------------------------------------------------------------------
# A6 — ci_failure_reproducer can infer the test runner from
# pytest/jest/go output patterns when no shell command appears in the log.
# ---------------------------------------------------------------------------


def test_a6_pytest_failed_summary_infers_pytest_command():
    """A bare 'FAILED tests/x.py::t' line should infer the pytest runner."""
    from agents import ci_failure_reproducer

    log = "FAILED tests/test_x.py::test_one - AssertionError: assert 1 == 2"
    cmd = ci_failure_reproducer._infer_command_from_output(log, None, [])
    assert cmd == "pytest"


def test_a6_jest_fail_summary_infers_npm_test():
    """A bare 'FAIL src/foo.test.js' should infer the jest runner."""
    from agents import ci_failure_reproducer

    log = "FAIL src/foo.test.js\n  ● foo › bar"
    cmd = ci_failure_reproducer._infer_command_from_output(log, None, [])
    assert cmd == "npm test"


def test_a6_go_test_summary_infers_go_test():
    """A bare '--- FAIL: TestFoo' should infer the go test runner."""
    from agents import ci_failure_reproducer

    log = "--- FAIL: TestFoo (0.01s)\n    foo_test.go:10: bad"
    cmd = ci_failure_reproducer._infer_command_from_output(log, None, [])
    assert cmd == "go test ./..."


def test_a6_language_hint_alone_infers_runner():
    """If no output signal, the language hint should still pick a runner."""
    from agents import ci_failure_reproducer

    cmd = ci_failure_reproducer._infer_command_from_output("", "python", [])
    assert cmd == "pytest"


# ---------------------------------------------------------------------------
# B7 — live_sandbox default boot image includes node + npm.
# ---------------------------------------------------------------------------


def test_b7_default_sandbox_image_includes_node():
    """The custom_commands default boot image must ship with node + npm."""
    from core.sandbox import boot

    # cimg/node:current explicitly bundles a Node.js runtime + npm on top
    # of cimg/base. Any other choice must also bundle node — assert
    # against the family rather than locking to the exact tag, but reject
    # the bare cimg/base that B7 found broken.
    assert "node" in boot._DEFAULT_CUSTOM_IMAGE, (
        f"_DEFAULT_CUSTOM_IMAGE is {boot._DEFAULT_CUSTOM_IMAGE!r} — must "
        "ship with a Node.js runtime for the default 'boot the user's repo' use case"
    )
    assert boot._DEFAULT_CUSTOM_IMAGE != "cimg/base:current", (
        "cimg/base is missing node — B7 regression"
    )


# ---------------------------------------------------------------------------
# B5 — host info-leak hardening. /proc/version, /proc/cpuinfo, and
# /etc/os-release are masked via bind-mount even on the default docker
# backend. The kernel uname syscall itself is acknowledged_limitation:
# gVisor is the only complete fix and lives behind isolation_backend=gvisor.
# ---------------------------------------------------------------------------


def test_b5_hardening_argv_includes_proc_masking():
    """Direct-launch boots must bind-mount masked /proc + /etc files."""
    from core.sandbox import isolation

    argv = isolation.hardening_argv("test_sandbox_id_abcdef0123")
    # Bind-mounts come as ``-v source:target:ro`` pairs.
    joined = " ".join(argv)
    for target in ("/proc/version", "/proc/cpuinfo", "/etc/os-release"):
        assert target in joined, (
            f"hardening_argv must bind-mount {target!r} — host info "
            "leaks (kernel build, AWS suffix, distro version) are useful "
            "recon for an attacker prepping a kernel-CVE escape"
        )


def test_b5_proc_mask_file_content_hides_host_kernel():
    """The masked /proc/version content must NOT mention the host kernel."""
    from core.sandbox import isolation

    mapping = isolation._ensure_proc_mask_files()
    for path in mapping.values():
        text = Path(path).read_text().lower()
        # The work order's leaked example: "6.17.0-1013-aws"
        assert "aws" not in text, f"{path} leaks the AWS host suffix"
        assert "ubuntu" not in text, f"{path} leaks the Ubuntu host distro"
        # Random version-like strings should not look like a real kernel.
        assert "1013" not in text, f"{path} appears to contain host kernel"


# ---------------------------------------------------------------------------
# D12 — sanitised PATH replaces parent PATH for every subprocess call,
# so multi_language_executor / python_code_executor can't leak the venv
# prefix (e.g. ``/home/aztea/app/venv/bin``) via ``process.env``.
# ---------------------------------------------------------------------------


def test_d12_build_subprocess_env_replaces_path_with_sanitised():
    """The parent PATH must never reach the child env unchanged."""
    import os

    from core import executor_sandbox

    leaky = "/home/aztea/app/venv/bin:/usr/bin"
    saved = os.environ.get("PATH")
    os.environ["PATH"] = leaky
    try:
        env = executor_sandbox.build_subprocess_env()
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    assert env["PATH"] == executor_sandbox._SANITISED_PATH
    assert "venv" not in env["PATH"]
    assert "home/aztea" not in env["PATH"]
