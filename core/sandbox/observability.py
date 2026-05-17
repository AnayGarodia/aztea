"""Logs, metrics, process inspection.

# OWNS: sandbox_logs (stream/tail/filter), sandbox_metrics (docker stats),
#       sandbox_inspect_process (ps + open files + env + cwd).
# NOT OWNS: receipts, audit, snapshots.
# INVARIANTS:
#   * Every output line is run through ``redact`` so a debug-mode service
#     that echoes a secret value doesn't leak into the agent response.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import (
    SandboxInvalidInput,
    SandboxServiceMissing,
)
from core.sandbox.secrets_store import all_secret_values, redact
from core.sandbox.state import SandboxState, get

_LOG = logging.getLogger("aztea.sandbox.observability")
_DEFAULT_TAIL = 500
_MAX_TAIL = 10_000
_LEVEL_PATTERNS = {
    "error": re.compile(r"\b(error|fatal|exception|panic)\b", re.IGNORECASE),
    "warn": re.compile(r"\b(warn|warning)\b", re.IGNORECASE),
    "info": re.compile(r"\b(info|notice)\b", re.IGNORECASE),
    "debug": re.compile(r"\bdebug\b", re.IGNORECASE),
}


def fetch_logs(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    service = str(payload.get("service") or "").strip()
    container = _container_for(state, service)
    tail = int(payload.get("tail") or _DEFAULT_TAIL)
    tail = max(1, min(tail, _MAX_TAIL))
    since = payload.get("since")
    argv = ["logs", "--tail", str(tail)]
    if since:
        argv.extend(["--since", str(since)])
    argv.append(container)
    proc = run_docker(argv, timeout=30, check=False)
    text = (proc.stdout or "") + (proc.stderr or "")
    text = redact(text, all_secret_values(state.sandbox_id))
    lines = text.splitlines()
    pattern = str(payload.get("regex") or "")
    level = str(payload.get("level") or "").strip().lower()
    if pattern:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise SandboxInvalidInput(f"invalid regex: {exc}") from exc
        lines = [ln for ln in lines if compiled.search(ln)]
    if level and level in _LEVEL_PATTERNS:
        lines = [ln for ln in lines if _LEVEL_PATTERNS[level].search(ln)]
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "service": service or "<default>",
        "lines": lines,
        "line_count": len(lines),
    }


def fetch_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    containers = [
        meta.get("container") or name
        for name, meta in state.boot.services.items()
    ]
    if not containers:
        raise SandboxServiceMissing("no services available for metrics")
    proc = run_docker(
        [
            "stats",
            "--no-stream",
            "--format",
            "{{json .}}",
            *containers,
        ],
        timeout=15,
        check=False,
    )
    per_service: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            per_service.append(json.loads(line))
        except ValueError:
            continue
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "metrics": per_service,
    }


def inspect_process(payload: dict[str, Any]) -> dict[str, Any]:
    state = _require(payload)
    service = str(payload.get("service") or "").strip()
    container = _container_for(state, service)
    pid = payload.get("pid")
    if pid is None:
        # Default: top-of-ps for the container.
        proc = run_docker(
            ["exec", container, "ps", "-eo", "pid,pcpu,pmem,rss,vsz,comm,args", "--sort=-pcpu"],
            timeout=10,
            check=False,
        )
        state.touch()
        return {
            "sandbox_id": state.sandbox_id,
            "service": service or "<default>",
            "ps": (proc.stdout or "")[:8000],
        }
    pid_str = str(int(pid))  # validates integer-ness
    # Collect cwd / env / open files via /proc/<pid>/*.
    cwd_proc = run_docker(
        ["exec", container, "readlink", "-f", f"/proc/{pid_str}/cwd"],
        timeout=5,
        check=False,
    )
    cmdline_proc = run_docker(
        ["exec", container, "cat", f"/proc/{pid_str}/cmdline"],
        timeout=5,
        check=False,
    )
    env_proc = run_docker(
        ["exec", container, "cat", f"/proc/{pid_str}/environ"],
        timeout=5,
        check=False,
    )
    fds_proc = run_docker(
        ["exec", container, "ls", "-l", f"/proc/{pid_str}/fd"],
        timeout=5,
        check=False,
    )
    state.touch()
    env_text = redact(env_proc.stdout or "", all_secret_values(state.sandbox_id))
    return {
        "sandbox_id": state.sandbox_id,
        "service": service or "<default>",
        "pid": int(pid),
        "cwd": (cwd_proc.stdout or "").strip(),
        "cmdline": (cmdline_proc.stdout or "").replace("\x00", " ").strip(),
        "environ": env_text.split("\x00") if env_text else [],
        "open_fds": (fds_proc.stdout or "")[:8000],
    }


def _container_for(state: SandboxState, service: str) -> str:
    """Pure: resolve the container name for ``service``; fallback to ``app``."""
    if service:
        meta = state.boot.services.get(service)
        if not meta:
            raise SandboxServiceMissing(
                f"service '{service}' not found; available: {sorted(state.boot.services)}"
            )
        return meta.get("container") or service
    for hint in ("app", "web", "api", "worker"):
        if hint in state.boot.services:
            return state.boot.services[hint].get("container") or hint
    if not state.boot.services:
        raise SandboxServiceMissing("no services available")
    first = next(iter(state.boot.services.values()))
    return first.get("container") or next(iter(state.boot.services))


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxInvalidInput(f"sandbox '{sandbox_id}' not active")
    return state
