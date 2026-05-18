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
import secrets
import shutil
import subprocess
from typing import Any

from core.sandbox.models import SandboxInvalidInput

_LOG = logging.getLogger("aztea.sandbox.isolation")

VALID_BACKENDS = ("docker", "gvisor", "firecracker", "kata")
_GVISOR_RUNTIME_NAME = "runsc"

# Bugs #5 / #6 / #7 from the 2026-05-18 audit: tighten the default
# ``docker run`` argv we generate for direct-launch boot strategies
# (dockerfile / custom_commands / devcontainer / nix). Compose stacks
# inherit their user/hostname/caps from the user's compose file — we
# don't second-guess that surface.
_DEFAULT_SANDBOX_UID = "1000:1000"
_HOSTNAME_PREFIX = "sandbox"


def hardening_argv(sandbox_id: str) -> list[str]:
    """Return the ``docker run`` flags that harden a direct-launch container.

    Drops to a non-root UID, masks the container ID as the hostname, and
    drops all Linux capabilities so a kernel CVE on the host cannot use
    CAP_SYS_ADMIN from inside. ``--security-opt no-new-privileges`` blocks
    setuid escalation paths. Why a separate function: every direct-launch
    boot path now flows through here so the security posture stays
    consistent — adding a new boot strategy that forgets to call this
    would regress all three bugs at once.

    NOTE: compose stacks bypass this on purpose — the user's compose
    file owns its own user/cap policy and overriding that breaks
    legitimate stacks (e.g. nginx wanting CAP_NET_BIND_SERVICE).
    """
    host_suffix = secrets.token_hex(4)
    sid_tail = sandbox_id.split("_", 1)[-1][:8] if "_" in sandbox_id else sandbox_id[:8]
    return [
        "--user", _DEFAULT_SANDBOX_UID,
        "--hostname", f"{_HOSTNAME_PREFIX}-{sid_tail}-{host_suffix}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]


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

    Bug #4 from the 2026-05-18 audit: the previous note implied every
    sandbox got runsc by default, but ``isolation.applied`` was always
    'docker' unless the caller explicitly asked for gVisor AND the host
    had runsc registered. The note now spells out the opt-in path so the
    response, the agent description, and operator reality all agree.
    """
    runsc_available = _gvisor_runtime_available()
    if backend == "gvisor":
        note = (
            "gVisor (runsc) is opt-in and was applied for this sandbox."
            if runsc_available
            else "gVisor requested but runsc is not registered on this host — "
            "this should have raised before container start; verify "
            "/etc/docker/daemon.json."
        )
    else:
        note = (
            "Default backend is plain Docker. gVisor (runsc) is opt-in via "
            "isolation_backend='gvisor' and only when the host has runsc "
            "registered — set runsc_available=true below to confirm. "
            "Firecracker / Kata remain host-infra rollouts."
        )
    return {
        "requested": backend,
        "applied": backend if backend in ("docker", "gvisor") else "refused",
        "runsc_available": runsc_available,
        "supported_backends": list(VALID_BACKENDS),
        "v0_real_backends": ["docker", "gvisor"],
        "note": note,
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
