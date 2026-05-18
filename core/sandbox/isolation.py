"""Isolation backend selection: docker (default), gVisor, firecracker, kata.

# OWNS: detecting which runtimes are usable on this host and translating
#       the caller-requested ``isolation_backend`` into the right
#       ``--runtime=<name>`` flag on every container spawn.
# NOT OWNS: actually installing the runtime. We refuse cleanly when the
#           caller asks for one the host can't supply.
# INVARIANTS:
#   * Default stays 'docker' — opt-in only.
#   * Firecracker + kata return a structured not-implemented envelope so
#     callers can see the surface without us pretending they're real.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from core.sandbox.models import SandboxInvalidInput

_LOG = logging.getLogger("aztea.sandbox.isolation")

VALID_BACKENDS = ("docker", "gvisor", "firecracker", "kata")
_GVISOR_RUNTIME_NAME = "runsc"


def normalise_backend(value: Any) -> str:
    """Pure: validate the caller's ``isolation_backend`` field."""
    text = str(value or "docker").strip().lower()
    if text not in VALID_BACKENDS:
        raise SandboxInvalidInput(
            f"isolation_backend must be one of {VALID_BACKENDS}; got {text!r}"
        )
    return text


def runtime_argv(backend: str) -> list[str]:
    """Return the ``docker run`` flags that select the requested runtime.

    Why: docker-cli ``--runtime=<name>`` is the cross-runtime escape hatch.
    gVisor registers as ``runsc``. For the runtimes we don't support we
    raise loudly so the caller knows before any container starts.
    """
    if backend == "docker":
        return []
    if backend == "gvisor":
        if not _gvisor_runtime_available():
            raise SandboxInvalidInput(
                "isolation_backend='gvisor' selected but the 'runsc' "
                "runtime is not registered with the Docker daemon. "
                "Install gVisor and add a runsc runtime entry under "
                "/etc/docker/daemon.json:runtimes, then restart docker."
            )
        return ["--runtime", _GVISOR_RUNTIME_NAME]
    # firecracker + kata: refuse loudly. We expose the surface so callers
    # can see what's planned, but never silently downgrade to docker.
    raise SandboxInvalidInput(
        f"isolation_backend='{backend}' is not implemented in this build. "
        "Firecracker and Kata require a host-level rollout (KVM, custom "
        "kernel, control-plane integration) outside the agent module — "
        "tracked as a separate infra issue. Use 'gvisor' for stronger "
        "isolation than vanilla Docker without that lift, or stay on "
        "the default 'docker' backend."
    )


def status_block(backend: str) -> dict[str, Any]:
    """Pure-ish: report what the engine resolved + what the host supports.

    Surfaced in the ``sandbox_start`` response so the caller sees whether
    they actually got the isolation they asked for.
    """
    return {
        "requested": backend,
        "applied": backend if backend in ("docker", "gvisor") else "refused",
        "runsc_available": _gvisor_runtime_available(),
        "supported_backends": list(VALID_BACKENDS),
        "v0_real_backends": ["docker", "gvisor"],
        "note": (
            "gVisor (runsc) provides strong syscall-level isolation on "
            "top of Docker. Firecracker / Kata remain host-infra rollouts."
        ),
    }


def _gvisor_runtime_available() -> bool:
    """Side-effect: probe whether the Docker daemon knows about ``runsc``."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    return _GVISOR_RUNTIME_NAME in (proc.stdout or "")
