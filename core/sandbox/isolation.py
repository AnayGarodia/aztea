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
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
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

# 2026-05-18 (B5): host info-leak mitigation. Without gVisor, docker
# containers share the host kernel — `uname -r` still returns the host
# kernel release. But /proc/version, /proc/cpuinfo, and /etc/os-release
# are caller-readable files we CAN override via bind-mount, which kills
# the most useful recon for an attacker prepping a kernel-CVE escape.
# The kernel uname syscall itself can only be masked with gVisor (see B3).
_PROC_MASK_VERSION = (
    "Linux version 6.0.0-aztea-sandbox (build@aztea) "
    "(gcc (sandbox)) #1 SMP\n"
)
_PROC_MASK_CPUINFO = (
    "processor\t: 0\n"
    "vendor_id\t: AzteaCPU\n"
    "cpu family\t: 0\n"
    "model\t\t: 0\n"
    "model name\t: Aztea Sandbox Virtual CPU\n"
    "stepping\t: 0\n"
    "cpu MHz\t\t: 0\n"
    "cache size\t: 0 KB\n"
)
_OS_RELEASE_MASK = (
    'NAME="Aztea Sandbox"\n'
    'PRETTY_NAME="Aztea Sandbox"\n'
    'ID=aztea-sandbox\n'
    'VERSION="1"\n'
    'VERSION_ID=1\n'
    'HOME_URL="https://aztea.ai/"\n'
)


def _ensure_proc_mask_files() -> dict[str, str]:
    """Side-effect: write masked /proc and /etc files to a stable host path.

    The files are world-readable, immutable from inside the container (bind-
    mounted readonly), and identical across sandbox starts. Cached on disk
    so we don't write them per-container.

    Returns a mapping of ``{container_target: host_source}`` for bind mounts.
    """
    base = Path(tempfile.gettempdir()) / "aztea-sandbox-mask"
    base.mkdir(mode=0o755, exist_ok=True)
    payloads = {
        "version": _PROC_MASK_VERSION,
        "cpuinfo": _PROC_MASK_CPUINFO,
        "os-release": _OS_RELEASE_MASK,
    }
    for name, content in payloads.items():
        path = base / name
        if not path.exists() or path.read_text() != content:
            path.write_text(content)
            os.chmod(path, 0o644)
    return {
        "/proc/version": str(base / "version"),
        "/proc/cpuinfo": str(base / "cpuinfo"),
        "/etc/os-release": str(base / "os-release"),
    }


def _proc_mask_argv() -> list[str]:
    """Return ``docker run`` flags that bind-mount fake /proc + /etc files.

    Why: vanilla docker shares the host kernel, so /proc/version exposes
    "Linux 6.17.0-1013-aws ..." which tells an attacker the host is on
    AWS running a specific kernel build — useful recon for a kernel-CVE
    escape. Bind-mounting our static fake files masks all three vectors
    in one place. Compose stacks bypass this (the user's compose file
    owns its own /proc policy — overriding it would break legitimate
    stacks that mount /proc themselves).
    """
    try:
        mapping = _ensure_proc_mask_files()
    except OSError as exc:  # disk full / permission — don't block boot
        _LOG.warning("could not prepare proc mask files: %s", exc)
        return []
    argv: list[str] = []
    for target, source in mapping.items():
        argv.extend(["-v", f"{source}:{target}:ro"])
    return argv


def hardening_argv(sandbox_id: str) -> list[str]:
    """Return the ``docker run`` flags that harden a direct-launch container.

    Drops to a non-root UID, masks the container ID as the hostname, drops
    all Linux capabilities so a kernel CVE on the host cannot use
    CAP_SYS_ADMIN from inside, blocks setuid escalation, and bind-mounts
    masked /proc + /etc files so the container can't read host kernel
    version / CPU model / OS release. Why a separate function: every
    direct-launch boot path now flows through here so the security posture
    stays consistent — adding a new boot strategy that forgets to call
    this would regress every hardening bug at once.

    NOTE: compose stacks bypass this on purpose — the user's compose
    file owns its own user/cap policy and overriding that breaks
    legitimate stacks (e.g. nginx wanting CAP_NET_BIND_SERVICE).
    KNOWN LIMITATION: the kernel `uname` syscall itself still returns the
    host kernel release. Only gVisor (isolation_backend='gvisor') masks it.
    """
    host_suffix = secrets.token_hex(4)
    sid_tail = sandbox_id.split("_", 1)[-1][:8] if "_" in sandbox_id else sandbox_id[:8]
    return [
        "--user", _DEFAULT_SANDBOX_UID,
        "--hostname", f"{_HOSTNAME_PREFIX}-{sid_tail}-{host_suffix}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        *_proc_mask_argv(),
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
