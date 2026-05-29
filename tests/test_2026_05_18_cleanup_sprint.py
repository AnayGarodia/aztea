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


def test_c3_e4_manage_workflow_inline_hire_async_summary_mentions_budget():
    """The hire_async one-liner inside manage_workflow's description must
    mention the async budget — that's what MCP clients using the lazy
    grouped surface see, NOT the sub-tool description. The 2026-05-19
    re-verification caught this gap: aztea_hire_async was updated but
    the inline summary was still just 'fire-and-poll a single long-
    running agent' with no budget.
    """
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    idx = src.find('"name": "manage_workflow"')
    assert idx >= 0, "manage_workflow tool block must exist"
    block = src[idx : idx + 4000]
    # Find the inline hire_async bullet inside the description.
    bullet_idx = block.find("• hire_async(slug")
    assert bullet_idx >= 0, "manage_workflow description must list hire_async"
    bullet = block[bullet_idx : bullet_idx + 700]
    assert "minutes" in bullet, (
        "manage_workflow's inline hire_async summary must mention the "
        "async budget in minutes"
    )
    assert "600" in bullet or "10 minutes" in bullet, (
        "manage_workflow's inline hire_async summary must reference the "
        "async default budget (600s / 10 min)"
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


def test_b5_hardening_argv_includes_etc_mask_but_not_proc():
    """Direct-launch boots must bind-mount /etc/os-release ONLY.

    runc's proc-safety check rejects bind-mounts inside /proc with
    `cannot be mounted because it is inside /proc`, returning rc=125
    on every sandbox boot. The earlier B5 patch tried to mount
    /proc/version and /proc/cpuinfo and broke sandbox_start across
    every direct-launch path. The fix is to drop the /proc mounts;
    full /proc masking requires gVisor (B3 roadmap).
    """
    from core.sandbox import isolation

    argv = isolation.hardening_argv("test_sandbox_id_abcdef0123")
    joined = " ".join(argv)
    # /etc/os-release: outside /proc, runc-safe, must be mounted.
    assert "/etc/os-release" in joined, (
        "hardening_argv must bind-mount /etc/os-release — distro fingerprint "
        "leak is still preventable on the default backend"
    )
    # /proc/* mounts must NOT be present — runc rejects them with rc=125.
    assert "/proc/version" not in joined, (
        "hardening_argv must NOT bind-mount /proc/version — runc proc-safety "
        "check fails the entire sandbox boot. Regression of the 2026-05-19 fix."
    )
    assert "/proc/cpuinfo" not in joined, (
        "hardening_argv must NOT bind-mount /proc/cpuinfo — runc proc-safety "
        "check fails the entire sandbox boot. Regression of the 2026-05-19 fix."
    )


def test_b5_etc_os_release_mask_content_hides_host_distro():
    """The masked /etc/os-release content must NOT mention real distros."""
    from core.sandbox import isolation

    mapping = isolation._ensure_proc_mask_files()
    # The mapping should ONLY include /etc/os-release post-2026-05-19.
    assert set(mapping.keys()) == {"/etc/os-release"}, (
        f"mask targets must be exactly /etc/os-release; got {set(mapping.keys())}"
    )
    text = Path(mapping["/etc/os-release"]).read_text().lower()
    for marker in ("ubuntu", "debian", "alpine", "aws"):
        assert marker not in text, (
            f"/etc/os-release mask leaks {marker!r} — host distro fingerprint"
        )


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


# ---------------------------------------------------------------------------
# C2 — hire_batch idempotency_key. The 2026-05-18 sprint shipped this as an
# acknowledged_limitation (per-job idempotency_key rejected with a useful
# hint). The 2026-05-19 follow-up implemented real server-side dedup at the
# top-level. This test now pins the new contract: per-job still rejected
# (since the key is per-batch), but the hint redirects to the top-level.
# ---------------------------------------------------------------------------


def test_c2_hire_batch_idempotency_key_rejection_includes_pointer():
    """The per-job rejection hint must point callers at the supported top-level."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # The deferral-era language ("is not implemented") is gone; the new
    # hint says the field is a TOP-LEVEL field on hire_batch.
    assert "TOP-LEVEL field" in src, (
        "hire_batch unsupported_field handler must special-case "
        "idempotency_key with a pointer that the field is top-level"
    )
    # And the hire_batch tool description must document the new RETRY SAFETY
    # contract — same idempotency_key + 24h dedup window.
    block_idx = src.find('"name": "aztea_hire_batch"')
    assert block_idx >= 0
    next_tool = src.find('"name": "aztea_', block_idx + 100)
    block = src[block_idx:next_tool] if next_tool > block_idx else src[block_idx:]
    assert "RETRY SAFETY" in block
    assert "within 24h" in block
    assert "idempotency.payload_mismatch" in block


# ---------------------------------------------------------------------------
# C19 — clarify error hint references request_message_id; the field is now
# present in manage_job's input schema so the hint is actionable.
# ---------------------------------------------------------------------------


def test_c19_manage_job_schema_documents_request_message_id():
    """Source-level check: manage_job schema entry includes request_message_id."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    # Locate the manage_job tool block.
    idx = src.find('"name": "manage_job"')
    assert idx >= 0, "manage_job tool block must exist"
    block = src[idx : idx + 6000]
    assert '"request_message_id"' in block, (
        "manage_job input_schema must list request_message_id so the clarify "
        "error hint 'Pass request_message_id explicitly if needed' is actionable"
    )


# ---------------------------------------------------------------------------
# C11 — modernize-python recipe was already removed from the search catalog.
# Pin that list_recipes and the SEARCH catalog agree on the same registry.
# ---------------------------------------------------------------------------


def test_c11_search_catalog_only_lists_real_recipes():
    """Every recipe slug in the SDK search catalog must back a real recipe."""
    from core import recipes

    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    idx = src.find("_BUILTIN_RECIPE_CATALOG_ENTRIES")
    assert idx >= 0
    # Pull each "id": "<slug>" from the catalog block.
    catalog_block = src[idx : idx + 4000]
    listed_slugs = set(re.findall(r'"id":\s*"([\w-]+)"', catalog_block))
    real_slugs = {r["recipe_id"] for r in recipes.BUILTIN_RECIPES}
    # Every listed slug must back a real recipe — surfacing a slug that
    # doesn't resolve is worse than not advertising it.
    missing = listed_slugs - real_slugs
    assert not missing, (
        f"search catalog lists {missing} which have no recipe backing — "
        "the modernize-python regression: callers see a slug in search "
        "but run_recipe returns 'Pipeline not found'"
    )


# ---------------------------------------------------------------------------
# B11 — sandbox_restore receipt prev_hash chains to the prior receipt
# (snapshot → exec → snapshot → restore). Source-level pin: build_receipt
# reads _last_hash(sandbox_id) so the chain is automatic per sandbox.
# ---------------------------------------------------------------------------


def test_b11_restore_receipt_uses_chain_tail():
    """build_receipt must pull prev_hash from _last_hash(sandbox_id)."""
    src = Path("core/sandbox/receipts.py").read_text()
    # The single source of truth — every receipt (including sandbox_restore)
    # routes through build_receipt, which sets prev_hash via _last_hash.
    assert "prev_hash = _last_hash(sandbox_id) if sandbox_id else \"\"" in src, (
        "build_receipt must derive prev_hash from _last_hash(sandbox_id) "
        "so the restore receipt's prev_hash chains to the snapshot receipt"
    )


# ---------------------------------------------------------------------------
# D4 — describe_agent tool description documents cache parameters
# (ttl_seconds, partition, invalidation trigger). Wave 2 rename: the tool
# was `describe_specialist` before 2026-05-26.
# ---------------------------------------------------------------------------


def test_d4_describe_agent_documents_cache_parameters():
    """describe_agent description must surface cache_ttl + partition."""
    src = Path("sdks/python-sdk/aztea/mcp/server.py").read_text()
    idx = src.find('"name": "describe_agent"')
    assert idx >= 0
    block = src[idx : idx + 6000]
    for marker in ("cache_ttl_seconds", "version_token", "cache_replay"):
        assert marker in block, (
            f"describe_agent must document {marker!r} so callers "
            "know the cache lifetime / partitioning / invalidation rules"
        )


# ---------------------------------------------------------------------------
# D5 — manage_workflow(session_audit) accepts include_receipts +
# include_spend_breakdown to shrink the response.
# ---------------------------------------------------------------------------


def test_d5_session_audit_schema_includes_size_toggles():
    """manage_workflow's session_audit schema must declare the size toggles."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    idx = src.find('"name": "manage_workflow"')
    assert idx >= 0
    block = src[idx : idx + 12000]
    assert '"include_receipts"' in block
    assert '"include_spend_breakdown"' in block


def test_d5_session_audit_handler_forwards_size_toggles():
    """_session_audit must forward include_receipts/include_spend_breakdown."""
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    idx = src.find("def _session_audit(")
    assert idx >= 0
    body = src[idx : idx + 2500]
    assert 'args.get("include_receipts")' in body
    assert 'args.get("include_spend_breakdown")' in body


# ---------------------------------------------------------------------------
# E2 — hire_batch description must document all-or-nothing slug resolution.
# Verified against the manage_workflow description (work-order text said
# this was likely already FIXED, just needs a pin).
# ---------------------------------------------------------------------------


def test_e2_hire_batch_description_documents_all_or_nothing():
    src = Path("sdks/python-sdk/aztea/mcp/meta_tools.py").read_text()
    idx = src.find('"name": "manage_workflow"')
    assert idx >= 0
    block = src[idx : idx + 6000]
    assert "all-or-nothing" in block.lower(), (
        "manage_workflow description must document the all-or-nothing "
        "slug-resolution rule for hire_batch"
    )
