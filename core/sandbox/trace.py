"""sandbox_trace — privileged process attach (py-spy / strace / perf).

# OWNS: spawning a profiler/strace sidecar that joins the target
#       container's PID namespace and attaches to ``pid``. Produces an
#       artifact (flamegraph for py-spy, raw stream for strace) on the
#       host filesystem.
# NOT OWNS: default-on privilege. Gated behind ``AZTEA_SANDBOX_ALLOW_PTRACE=1``.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import SandboxInvalidInput, SandboxNotFound, SandboxServiceMissing
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.trace")
_PTRACE_FLAG = "AZTEA_SANDBOX_ALLOW_PTRACE"
_DEFAULT_DURATION_S = 20
_HARD_MAX_DURATION_S = 300
_VALID_TOOLS = ("py-spy", "strace", "perf", "async-profiler")
_DEFAULT_IMAGES = {
    "py-spy": "benfred/py-spy:latest",
    "strace": "nicolaka/netshoot:latest",
    "perf": "nicolaka/netshoot:latest",
    "async-profiler": "nicolaka/netshoot:latest",
}


def trace(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach a profiler/tracer to a PID inside the target service container.

    Why: "why is this slow?" and "why does this server think it's in
    prod mode?" both need real attach. Gated behind PTRACE so the
    default-deny posture stays for hosts that don't want it.
    """
    state = _require(payload)
    if os.environ.get(_PTRACE_FLAG, "") != "1":
        return _refused_envelope(state)
    service, pid, tool, duration = _validate_trace_input(state, payload)
    output_dir = sandbox_dir(state.sandbox_id) / "traces"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    container = state.boot.services[service]["container"]
    if tool == "py-spy":
        return _trace_pyspy(state, container, pid, duration, output_dir)
    if tool == "strace":
        return _trace_strace(state, container, pid, duration, output_dir)
    # perf / async-profiler are stubbed honestly behind the same env: we
    # don't bundle JVM tooling and perf needs kernel debug headers that
    # vary by host. Tell the operator what's missing.
    return {
        "sandbox_id": state.sandbox_id,
        "tool": tool,
        "supported_in_this_build": False,
        "reason": (
            f"'{tool}' is reachable behind {_PTRACE_FLAG} but the bundled "
            "sidecar image only ships py-spy and strace. Add a custom "
            f"image to override via payload['image'] or use tool='py-spy'."
        ),
    }


def _refused_envelope(state: SandboxState) -> dict[str, Any]:
    """Pure: structured refusal mirroring network_capture's shape."""
    return {
        "sandbox_id": state.sandbox_id,
        "elevated": False,
        "refused": True,
        "reason": (
            f"sandbox_trace requires SYS_PTRACE which is gated behind "
            f"{_PTRACE_FLAG}=1 in the server environment. Set it on the "
            "host that runs the Aztea server, restart, retry."
        ),
        "next_step": f"export {_PTRACE_FLAG}=1 && restart the server",
    }


def _trace_pyspy(
    state: SandboxState,
    container: str,
    pid: int,
    duration: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Side-effect: run py-spy in record mode joined to the container's PID namespace."""
    artifact = output_dir / f"flame_{secrets.token_hex(4)}.svg"
    image = _DEFAULT_IMAGES["py-spy"]
    sidecar = f"aztea-pyspy-{state.sandbox_id[-8:]}-{secrets.token_hex(3)}"
    bind_dir = str(output_dir)
    proc = run_docker(
        [
            "run", "--rm",
            "--name", sidecar,
            "--pid", f"container:{container}",
            "--cap-add=SYS_PTRACE",
            "-v", f"{bind_dir}:/out",
            image,
            "py-spy", "record",
            "--pid", str(pid),
            "--duration", str(duration),
            "--output", f"/out/{artifact.name}",
            "--format", "flamegraph",
        ],
        timeout=duration + 30,
        check=False,
    )
    if proc.returncode != 0:
        raise SandboxInvalidInput(
            f"py-spy failed (rc={proc.returncode}): "
            f"{(proc.stderr or '')[:512]}"
        )
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "tool": "py-spy",
        "pid": pid,
        "service": _service_from_container(state, container),
        "artifact_path": str(artifact),
        "artifact_format": "svg-flamegraph",
        "duration_seconds": duration,
        "elevated": True,
        "summary": "py-spy flamegraph captured; open the SVG in any browser.",
    }


def _trace_strace(
    state: SandboxState,
    container: str,
    pid: int,
    duration: int,
    output_dir: Path,
) -> dict[str, Any]:
    """Side-effect: run a time-bounded strace, summarising syscall histogram."""
    image = _DEFAULT_IMAGES["strace"]
    sidecar = f"aztea-strace-{state.sandbox_id[-8:]}-{secrets.token_hex(3)}"
    artifact = output_dir / f"strace_{secrets.token_hex(4)}.txt"
    bind_dir = str(output_dir)
    # -c counts syscalls; -p attaches; timeout caps wall clock.
    proc = run_docker(
        [
            "run", "--rm",
            "--name", sidecar,
            "--pid", f"container:{container}",
            "--cap-add=SYS_PTRACE",
            "-v", f"{bind_dir}:/out",
            image,
            "sh", "-lc",
            f"timeout {duration} strace -c -f -p {pid} -o /out/{artifact.name} || true",
        ],
        timeout=duration + 30,
        check=False,
    )
    if proc.returncode != 0:
        raise SandboxInvalidInput(
            f"strace sidecar failed (rc={proc.returncode}): "
            f"{(proc.stderr or '')[:512]}"
        )
    summary = ""
    if artifact.is_file():
        try:
            summary = artifact.read_text("utf-8", "replace")[:4096]
        except OSError:
            summary = ""
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "tool": "strace",
        "pid": pid,
        "service": _service_from_container(state, container),
        "artifact_path": str(artifact),
        "artifact_format": "strace-c-summary",
        "duration_seconds": duration,
        "summary": summary,
        "elevated": True,
    }


def _validate_trace_input(
    state: SandboxState, payload: dict[str, Any],
) -> tuple[str, int, str, int]:
    service = str(payload.get("service") or "").strip()
    if not service:
        raise SandboxInvalidInput("service is required for sandbox_trace")
    if service not in state.boot.services:
        raise SandboxServiceMissing(
            f"service '{service}' not found; available: {sorted(state.boot.services)}"
        )
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError) as exc:
        raise SandboxInvalidInput("pid must be a positive integer") from exc
    if pid <= 0:
        raise SandboxInvalidInput("pid must be a positive integer")
    tool = str(payload.get("tool") or "py-spy").strip().lower()
    if tool not in _VALID_TOOLS:
        raise SandboxInvalidInput(
            f"tool must be one of {_VALID_TOOLS}; got {tool!r}"
        )
    try:
        duration = int(payload.get("duration_seconds") or _DEFAULT_DURATION_S)
    except (TypeError, ValueError) as exc:
        raise SandboxInvalidInput("duration_seconds must be an integer") from exc
    if duration <= 0:
        raise SandboxInvalidInput("duration_seconds must be > 0")
    if duration > _HARD_MAX_DURATION_S:
        raise SandboxInvalidInput(
            f"duration_seconds capped at {_HARD_MAX_DURATION_S}"
        )
    return service, pid, tool, duration


def _service_from_container(state: SandboxState, container: str) -> str | None:
    """Pure: reverse-lookup the service name for a container."""
    for name, meta in state.boot.services.items():
        if meta.get("container") == container or name == container:
            return name
    return None


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state
