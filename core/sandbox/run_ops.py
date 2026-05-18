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
    stop_when = opts.get("stop_when") if isinstance(opts.get("stop_when"), str) else None
    stream = bool(opts.get("stream"))
    # Audit 2026-05-17 gap #10: streaming + stop_when. When the caller
    # asks for line-level capture or an early-terminate predicate, drive
    # the subprocess directly so we can read each line as it lands.
    if stream or stop_when:
        return _stream_in_container(
            state, argv, stdin=stdin, timeout=timeout, started_at=start,
            stop_when=stop_when,
        )
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


def _stream_in_container(
    state: SandboxState,
    argv: list[str],
    *,
    stdin: str | None,
    timeout: int,
    started_at: float,
    stop_when: str | None,
) -> dict[str, Any]:
    """Side-effect: stream stdout line-by-line; honour stop_when early-terminate.

    Why: the synchronous run_docker waits for the subprocess to exit
    before returning. For long test runs the agent wants to early-
    terminate the moment a failure line appears. We Popen directly,
    iterate stdout, match each line against stop_when, and kill on hit.
    """
    import re
    import subprocess

    from core.sandbox.docker_cli import docker_binary

    pattern: re.Pattern[str] | None = None
    if stop_when:
        try:
            pattern = re.compile(stop_when)
        except re.error as exc:
            raise SandboxInvalidInput(f"invalid stop_when regex: {exc}") from exc
    full = [docker_binary(), *argv]
    events: list[dict[str, Any]] = []
    stdout_lines: list[str] = []
    stop_hit_line: str | None = None
    proc = subprocess.Popen(  # noqa: S603
        full,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge so the line stream is one channel
        text=True,
    )
    if stdin is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except Exception:
            pass
    deadline = started_at + timeout
    timed_out = False
    early_terminated = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if time.time() > deadline:
                timed_out = True
                proc.kill()
                break
            line = line.rstrip("\n")
            stdout_lines.append(line)
            events.append({"ts": int(time.time() * 1000), "line": line[:2000]})
            if pattern is not None and pattern.search(line):
                stop_hit_line = line
                early_terminated = True
                proc.kill()
                break
        rc = proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
        timed_out = True
    duration_ms = int((time.time() - started_at) * 1000)
    state.touch()
    secret_values = all_secret_values(state.sandbox_id)
    stdout_text = "\n".join(stdout_lines)
    return {
        "sandbox_id": state.sandbox_id,
        "stdout": redact(stdout_text, secret_values)[:64_000],
        "stderr": "",
        "exit_code": 124 if timed_out else (int(rc or 0)),
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "events": events[-500:],
        "stop_when_matched": stop_hit_line,
        "early_terminated": early_terminated,
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
        "stream": payload.get("stream"),
        "stop_when": payload.get("stop_when"),
    }
    if opts["env"] and not isinstance(opts["env"], dict):
        raise SandboxInvalidInput("exec.env must be an object")
    if opts["stop_when"] is not None and not isinstance(opts["stop_when"], str):
        raise SandboxInvalidInput("stop_when must be a regex string")
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
