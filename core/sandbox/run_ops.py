"""sandbox_exec, sandbox_exec_in_service, sandbox_bg_* — command runners.

# OWNS: every "run a command inside the sandbox" surface. All argv runs go
#       through docker via argv lists, never via host shell.
# INVARIANTS:
#   * Response fields exactly match Claude Code's local Bash tool:
#     stdout, stderr, exit_code, timed_out, duration_ms. No additions to
#     that tuple (extras live alongside, never as renames).
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import (
    DEFAULT_EXEC_TIMEOUT_S,
    HARD_EXEC_TIMEOUT_S,
    SandboxInvalidInput,
    SandboxServiceMissing,
)
from core.sandbox.secrets_store import all_secret_values, redact
from core.sandbox.state import SandboxState, get

_LOG = logging.getLogger("aztea.sandbox.run_ops")

DOCKER_EXEC = "exec"


def run_command(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    cmd, opts = _validate_run(payload)
    container = _default_service_container(state)
    return _run_in_container(state, container, cmd, opts)


def run_command_in_service(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    service = str(payload.get("service") or "").strip()
    if not service:
        raise SandboxInvalidInput("service is required for sandbox_exec_in_service")
    if service not in state.boot.services:
        raise SandboxServiceMissing(
            f"service '{service}' not found; available: {sorted(state.boot.services)}"
        )
    cmd, opts = _validate_run(payload)
    container = state.boot.services[service].get("container") or service
    return _run_in_container(state, container, cmd, opts)


def bg_start(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        raise SandboxInvalidInput("cmd is required for sandbox_bg_start")
    name_hint = str(payload.get("name") or "").strip()
    bg_id = f"bg_{secrets.token_hex(6)}"
    container = _default_service_container(state)
    proc = run_docker(
        [DOCKER_EXEC, "--detach", container, "sh", "-lc", cmd],
        timeout=15,
    )
    state.bg_processes[bg_id] = {
        "bg_id": bg_id,
        "name": name_hint or bg_id,
        "cmd": cmd,
        "container": container,
        "started_at": int(time.time()),
        "handle": proc.stdout.strip()[:64],
    }
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "bg_id": bg_id,
        "name": state.bg_processes[bg_id]["name"],
        "started_at": state.bg_processes[bg_id]["started_at"],
    }


def bg_list(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    return {
        "sandbox_id": state.sandbox_id,
        "bg_processes": list(state.bg_processes.values()),
    }


def bg_kill(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    bg_id = str(payload.get("bg_id") or "").strip()
    entry = state.bg_processes.pop(bg_id, None)
    if entry is None:
        raise SandboxInvalidInput(f"bg_id '{bg_id}' not found")
    container = entry.get("container") or _default_service_container(state)
    quoted = _shell_quote(entry.get("cmd") or "")
    run_docker(
        [DOCKER_EXEC, container, "sh", "-lc", f"pkill -f {quoted} || true"],
        timeout=10,
        check=False,
    )
    state.touch()
    return {"sandbox_id": state.sandbox_id, "bg_id": bg_id, "killed": True}


def bg_logs(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    bg_id = str(payload.get("bg_id") or "").strip()
    entry = state.bg_processes.get(bg_id)
    if entry is None:
        raise SandboxInvalidInput(f"bg_id '{bg_id}' not found")
    container = entry.get("container") or _default_service_container(state)
    proc = run_docker(
        ["logs", "--tail", "500", container],
        timeout=10,
        check=False,
    )
    return {
        "sandbox_id": state.sandbox_id,
        "bg_id": bg_id,
        "stdout": redact(proc.stdout or "", all_secret_values(state.sandbox_id)),
        "stderr": redact(proc.stderr or "", all_secret_values(state.sandbox_id)),
    }


def _run_in_container(
    state: SandboxState,
    container: str,
    cmd: str,
    opts: dict[str, Any],
) -> dict[str, Any]:
    argv = [DOCKER_EXEC]
    if opts.get("user"):
        argv.extend(["--user", str(opts["user"])])
    cwd = opts.get("cwd")
    if cwd:
        argv.extend(["--workdir", str(cwd)])
    for k, v in (opts.get("env") or {}).items():
        argv.extend(["--env", f"{k}={v}"])
    argv.append(container)
    argv.extend(["sh", "-lc", cmd])
    start = time.time()
    stdin = opts.get("stdin")
    timeout = int(opts.get("timeout_seconds") or DEFAULT_EXEC_TIMEOUT_S)
    timeout = max(1, min(timeout, HARD_EXEC_TIMEOUT_S))
    timed_out = False
    try:
        proc = run_docker(argv, stdin=stdin, timeout=timeout, check=False)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = int(proc.returncode or 0)
    except Exception as exc:  # noqa: BLE001
        stdout = ""
        stderr = str(exc)
        exit_code = 124
        timed_out = True
    duration_ms = int((time.time() - start) * 1000)
    state.touch()
    secret_values = all_secret_values(state.sandbox_id)
    return {
        "sandbox_id": state.sandbox_id,
        "stdout": redact(stdout, secret_values)[:64_000],
        "stderr": redact(stderr, secret_values)[:32_000],
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
    }


def _validate_run(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Pure-ish: validate the Bash-shape input; returns (cmd, opts)."""
    cmd = str(payload.get("cmd") or "").strip()
    if not cmd:
        raise SandboxInvalidInput("cmd is required for sandbox_exec")
    opts: dict[str, Any] = {
        "cwd": payload.get("cwd"),
        "stdin": payload.get("stdin"),
        "env": payload.get("env") or {},
        "user": payload.get("user"),
        "timeout_seconds": payload.get("timeout_seconds"),
    }
    if opts["env"] and not isinstance(opts["env"], dict):
        raise SandboxInvalidInput("exec.env must be an object")
    return cmd, opts


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return state


def _default_service_container(state: SandboxState) -> str:
    """Pure: pick the canonical container for sandbox_exec (no service arg)."""
    services = state.boot.services
    if not services:
        raise SandboxServiceMissing(
            "no services available; sandbox may not have booted any containers"
        )
    for preferred in ("app", "web", "api", "worker"):
        if preferred in services:
            return services[preferred].get("container") or preferred
    first = next(iter(services.values()))
    return first.get("container") or next(iter(services))


def _shell_quote(value: str) -> str:
    """Pure: minimal shell-quote for pkill argument."""
    return "'" + str(value).replace("'", "'\\''") + "'"
