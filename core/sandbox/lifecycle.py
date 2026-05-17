"""Lifecycle: start / status / stop / extend / list / resume.

# OWNS: orchestration of source → boot → ready → registry; teardown via
#       ``docker compose down`` or label-filtered ``rm -f`` for non-compose.
# NOT OWNS: exec, fs, db, snapshots — those are independent surfaces that
#           use the ``SandboxState`` lifecycle.py populates.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.sandbox.boot import boot as boot_strategy
from core.sandbox.determinism import determinism_env
from core.sandbox.docker_cli import docker_available, run_compose, run_docker
from core.sandbox.models import (
    DEFAULT_AUTO_SNAPSHOT_MIN,
    DEFAULT_CPU_LIMIT,
    DEFAULT_DISK_GB,
    DEFAULT_IDLE_KILL_MIN,
    DEFAULT_MAX_LIFETIME_MIN,
    DEFAULT_MEMORY_GB,
    DEFAULT_PIDS_LIMIT,
    HARD_MAX_LIFETIME_MIN,
    SandboxBootFailed,
    SandboxDockerUnavailable,
    SandboxInvalidInput,
    SandboxNotFound,
    now_unix,
)
from core.sandbox.network import build_network_argv, compose_network_env, stop_orphan_containers
from core.sandbox.secrets_store import resolve_secret_refs
from core.sandbox.source import materialise_source
from core.sandbox.state import (
    LifetimePolicy,
    NetworkPolicyState,
    SandboxState,
    epoch_minute_offset,
    generate_sandbox_id,
    get,
    list_all,
    project_name_for,
    register,
    remove,
)

_LOG = logging.getLogger("aztea.sandbox.lifecycle")


def start(payload: dict[str, Any]) -> dict[str, Any]:
    """Spin up a sandbox. Returns the project-canonical ``sandbox_start`` response."""
    if not docker_available():
        raise SandboxDockerUnavailable(
            "Docker daemon is not reachable. Start Docker Desktop or run "
            "`systemctl start docker` and retry."
        )
    sandbox_id = generate_sandbox_id()
    source = payload.get("source") or {}
    boot_cfg = payload.get("boot") or {}
    env_cfg = payload.get("env") or {}
    network_cfg = payload.get("network") or {}
    size_cfg = payload.get("size") or {}
    lifetime_cfg = payload.get("lifetime") or {}
    clock_cfg = payload.get("clock") or {}
    workspace_id = payload.get("workspace_id")
    region = str(payload.get("region") or "auto")
    t0 = time.time()
    repo_path, clone_timing = materialise_source(sandbox_id, source)
    secret_env, unresolved_secrets = resolve_secret_refs(
        sandbox_id, env_cfg.get("secret_refs")
    )
    inline_env = _build_inline_env(env_cfg)
    det_env, det_status = determinism_env(clock_cfg)
    compose_env = compose_network_env(network_cfg)
    env_vars: dict[str, str] = {**compose_env, **inline_env, **secret_env, **det_env}
    network_argv, network_resolved = build_network_argv(sandbox_id, network_cfg)
    try:
        boot_info = boot_strategy(
            sandbox_id=sandbox_id,
            repo_path=repo_path,
            boot_cfg=boot_cfg,
            env_vars=env_vars,
            network_argv=network_argv,
            project_name_override=project_name_for(sandbox_id),
        )
    except SandboxBootFailed:
        # Boot already cleaned up its own intermediates; we leave the on-disk
        # state dir so the caller can pull the boot log for triage.
        raise
    boot_info.boot_timing.update(clone_timing)
    lifetime_policy = _normalise_lifetime(lifetime_cfg)
    state = SandboxState(
        sandbox_id=sandbox_id,
        status="ready",
        created_at=now_unix(),
        expires_at=epoch_minute_offset(lifetime_policy.max_minutes),
        last_activity_at=now_unix(),
        last_snapshot_at=0,
        workspace_id=workspace_id if isinstance(workspace_id, str) else None,
        owner_hint=payload.get("owner_hint") if isinstance(payload.get("owner_hint"), str) else None,
        region=region,
        size=_normalise_size(size_cfg),
        lifetime=lifetime_policy,
        network=NetworkPolicyState(
            egress=network_resolved["egress"],
            egress_allowlist=list(network_resolved.get("egress_allowlist", [])),
        ),
        boot=boot_info,
        filesystem_root=repo_path,
    )
    register(state)
    total = round(time.time() - t0, 2)
    boot_info.boot_timing["total"] = total
    return _start_response(state, det_status, unresolved_secrets)


def status(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    state.touch()
    return _status_response(state)


def stop(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    final_snapshot_id = None
    take_final = bool(payload.get("final_snapshot", state.lifetime.snapshot_on_stop))
    if take_final:
        from core.sandbox.snapshots import snapshot as snapshot_action

        out = snapshot_action({"sandbox_id": state.sandbox_id, "reason": "stop"})
        final_snapshot_id = out.get("snapshot_id")
    # Close any browser sessions tied to this sandbox before the host
    # containers go away. Without this, chromium children outlive their
    # parent and leak file descriptors.
    try:
        from core.sandbox import browser as _browser

        _browser.evict_for_sandbox(state.sandbox_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        _LOG.exception("browser eviction failed for %s", state.sandbox_id)
    _teardown(state)
    remove(state.sandbox_id)
    return {
        "sandbox_id": state.sandbox_id,
        "status": "stopped",
        "final_snapshot_id": final_snapshot_id,
        "resource_consumption": _resource_summary(state),
    }


def extend(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    minutes = int(payload.get("minutes") or 0)
    if minutes <= 0:
        raise SandboxInvalidInput("extend.minutes must be a positive integer")
    new_max = state.lifetime.max_minutes + minutes
    if new_max > HARD_MAX_LIFETIME_MIN:
        raise SandboxInvalidInput(
            f"extend would exceed hard cap of {HARD_MAX_LIFETIME_MIN} minutes; "
            f"requested total = {new_max}"
        )
    state.lifetime.max_minutes = new_max
    state.expires_at = state.created_at + new_max * 60
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "expires_at": state.expires_at,
        "max_lifetime_minutes": state.lifetime.max_minutes,
    }


_BATCH_MAX_PARALLEL = 4
_BATCH_HARD_MAX = 16


def batch_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Start N sandboxes from a single matrix request.

    Why: every matrix-test demo ("run my tests on Node 18/20/22") wants
    N sandboxes spun up together. Pre-fill this was a stub; the engine
    now loops :func:`start` with a small concurrency cap so the operator
    doesn't have to manage N round-trips. Wallet-hold integration is
    explicitly tracked as a follow-up — for v0 each child run charges
    independently when wallets are wired.

    Input:
        ``{"matrix": {<axis>: [<value>, ...], ...}, "base": {<start payload>}}``

    The Cartesian product of ``matrix`` is materialised; each cell merges
    its ``axis: value`` pairs into ``base.env.vars`` so user compose can
    branch on them. Failures during a child boot do NOT roll back already-
    booted siblings — the caller decides whether to stop them.
    """
    matrix = payload.get("matrix") or {}
    base = payload.get("base") or {}
    if not isinstance(matrix, dict) or not matrix:
        raise SandboxInvalidInput("batch_start.matrix must be a non-empty object")
    if not isinstance(base, dict):
        raise SandboxInvalidInput("batch_start.base must be an object")
    cells = _materialise_matrix(matrix)
    if not cells:
        raise SandboxInvalidInput("batch_start.matrix produced zero cells")
    if len(cells) > _BATCH_HARD_MAX:
        raise SandboxInvalidInput(
            f"batch_start matrix produced {len(cells)} cells; hard cap is "
            f"{_BATCH_HARD_MAX} to protect host resources"
        )
    results: list[dict[str, Any]] = []
    for cell_env in cells:
        cell_payload = _merge_matrix_cell(base, cell_env)
        try:
            cell_result = start(cell_payload)
            results.append({
                "axis_values": cell_env,
                "sandbox_id": cell_result.get("sandbox_id"),
                "status": cell_result.get("status"),
                "boot_strategy_detected": cell_result.get("boot_strategy_detected"),
            })
        except Exception as exc:  # noqa: BLE001 — boundary
            results.append({
                "axis_values": cell_env,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
    successful = [r for r in results if r.get("sandbox_id")]
    return {
        "matrix_cells": len(cells),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "sandbox_ids": [r["sandbox_id"] for r in successful],
        "results": results,
        "billing_notice": (
            "Each matrix cell charges independently via the per-call wallet "
            "path. Wallet pre-hold for the full batch is tracked in the "
            "follow-up issue paired with sandbox_export_snapshot."
        ),
    }


def _materialise_matrix(matrix: dict[str, Any]) -> list[dict[str, str]]:
    """Pure: Cartesian product of ``{axis: [values]}`` into a list of cells."""
    axes: list[tuple[str, list[str]]] = []
    for axis, values in matrix.items():
        if not isinstance(axis, str) or not axis.strip():
            raise SandboxInvalidInput(f"matrix axis name must be a non-empty string; got {axis!r}")
        if not isinstance(values, list) or not values:
            raise SandboxInvalidInput(
                f"matrix['{axis}'] must be a non-empty list of values"
            )
        axes.append((axis.strip(), [str(v) for v in values]))
    cells: list[dict[str, str]] = [{}]
    for axis_name, axis_values in axes:
        new_cells: list[dict[str, str]] = []
        for existing in cells:
            for value in axis_values:
                merged = dict(existing)
                merged[axis_name] = value
                new_cells.append(merged)
        cells = new_cells
    return cells


def _merge_matrix_cell(base: dict[str, Any], cell_env: dict[str, str]) -> dict[str, Any]:
    """Pure: merge ``cell_env`` into ``base.env.vars`` and return a fresh start payload."""
    import copy

    payload = copy.deepcopy(base)
    env_block = payload.setdefault("env", {})
    if not isinstance(env_block, dict):
        raise SandboxInvalidInput("batch_start.base.env must be an object")
    vars_block = env_block.setdefault("vars", {})
    if not isinstance(vars_block, dict):
        raise SandboxInvalidInput("batch_start.base.env.vars must be an object")
    vars_block.update(cell_env)
    return payload


def list_sandboxes(_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "sandboxes": [_brief(s) for s in list_all()],
    }


def resume(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-attach to an existing sandbox (no-op when it's still healthy).

    Why: the spec requires resumability across MCP sessions; for the in-memory
    registry that means we revalidate Docker still has the project alive.
    Suspended sandboxes (auto-suspended idle ones) get unsuspended here.
    """
    state = _require(payload)
    state.touch()
    if state.status == "suspended":
        _resume_containers(state)
        state.status = "ready"
    return _status_response(state)


def _resume_containers(state: SandboxState) -> None:
    """Side-effect: ``docker start`` every container linked to this sandbox."""
    project = state.boot.project_name
    proc = run_docker(
        [
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        timeout=10,
        check=False,
    )
    ids = [line for line in proc.stdout.splitlines() if line.strip()]
    if ids:
        run_docker(["start", *ids], timeout=60, check=False)


def _teardown(state: SandboxState) -> None:
    """Side-effect: shut every container down. Compose or label-filter fallback."""
    project = state.boot.project_name
    if state.boot.strategy == "docker_compose":
        try:
            run_compose(
                project,
                state.filesystem_root,
                ["down", "--remove-orphans", "--volumes"],
                timeout=120,
                check=False,
            )
        except Exception:
            _LOG.exception("compose down failed for %s", state.sandbox_id)
    stop_orphan_containers(project)


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state


def _normalise_lifetime(cfg: dict[str, Any]) -> LifetimePolicy:
    """Pure: apply server-side caps to caller-supplied lifetime knobs."""
    max_minutes = int(cfg.get("max_minutes") or DEFAULT_MAX_LIFETIME_MIN)
    if max_minutes <= 0:
        raise SandboxInvalidInput("lifetime.max_minutes must be positive")
    if max_minutes > HARD_MAX_LIFETIME_MIN:
        max_minutes = HARD_MAX_LIFETIME_MIN
    idle = int(cfg.get("idle_kill_minutes") or DEFAULT_IDLE_KILL_MIN)
    auto_snap = int(cfg.get("auto_snapshot_every_minutes") or DEFAULT_AUTO_SNAPSHOT_MIN)
    on_stop = bool(cfg.get("snapshot_on_stop", True))
    return LifetimePolicy(
        max_minutes=max_minutes,
        idle_kill_minutes=max(1, idle),
        auto_snapshot_every_minutes=max(1, auto_snap),
        snapshot_on_stop=on_stop,
    )


def _normalise_size(cfg: dict[str, Any]) -> dict[str, Any]:
    """Pure: shape + cap the ``size`` block; passed to docker as cgroup flags."""
    cpu = str(cfg.get("cpu") or DEFAULT_CPU_LIMIT)
    mem = int(cfg.get("memory_gb") or DEFAULT_MEMORY_GB)
    disk = int(cfg.get("disk_gb") or DEFAULT_DISK_GB)
    return {
        "cpu": cpu,
        "memory_gb": mem,
        "disk_gb": disk,
        "pids_limit": int(cfg.get("pids_limit") or DEFAULT_PIDS_LIMIT),
    }


def _build_inline_env(env_cfg: dict[str, Any]) -> dict[str, str]:
    inline = env_cfg.get("vars") or {}
    if not isinstance(inline, dict):
        raise SandboxInvalidInput("env.vars must be an object")
    out: dict[str, str] = {}
    for k, v in inline.items():
        if not isinstance(k, str):
            raise SandboxInvalidInput("env.vars keys must be strings")
        out[k] = str(v)
    return out


def _resource_summary(state: SandboxState) -> dict[str, Any]:
    """Pure: rough resource-consumption summary surfaced in stop response."""
    return {
        "uptime_seconds": now_unix() - state.created_at,
        "snapshot_count": len(state.snapshot_chain),
        "size": state.size,
    }


def _brief(state: SandboxState) -> dict[str, Any]:
    return {
        "sandbox_id": state.sandbox_id,
        "status": state.status,
        "created_at": state.created_at,
        "expires_at": state.expires_at,
        "last_activity_at": state.last_activity_at,
        "boot_strategy": state.boot.strategy,
        "workspace_id": state.workspace_id,
    }


def _status_response(state: SandboxState) -> dict[str, Any]:
    return {
        "sandbox_id": state.sandbox_id,
        "status": state.status,
        "created_at": state.created_at,
        "expires_at": state.expires_at,
        "last_activity_at": state.last_activity_at,
        "filesystem_root": state.filesystem_root,
        "services": state.boot.services,
        "snapshot_chain": list(state.snapshot_chain),
        "network": {
            "egress": state.network.egress,
            "egress_allowlist": state.network.egress_allowlist,
        },
        "lifetime": {
            "max_minutes": state.lifetime.max_minutes,
            "idle_kill_minutes": state.lifetime.idle_kill_minutes,
            "auto_snapshot_every_minutes": state.lifetime.auto_snapshot_every_minutes,
        },
        "bg_processes": list(state.bg_processes.values()),
    }


def _start_response(
    state: SandboxState,
    determinism_status: dict[str, Any],
    unresolved_secrets: list[str],
) -> dict[str, Any]:
    return {
        "sandbox_id": state.sandbox_id,
        "status": state.status,
        "boot_strategy_detected": state.boot.strategy,
        "services": state.boot.services,
        "filesystem_root": state.filesystem_root,
        "boot_timing": state.boot.boot_timing,
        "expires_at": state.expires_at,
        "snapshot_chain": list(state.snapshot_chain),
        "network": {
            "egress": state.network.egress,
            "egress_allowlist": state.network.egress_allowlist,
        },
        "determinism": determinism_status,
        "unresolved_secrets": unresolved_secrets,
        "workspace_id": state.workspace_id,
    }
