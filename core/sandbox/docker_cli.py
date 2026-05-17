"""Thin, shell-safe wrappers around the Docker CLI used by every surface.

# OWNS: subprocess invocation of ``docker`` and ``docker compose``; output
#       capture; timeout handling; error wrapping into ``SandboxError``.
# NOT OWNS: any sandbox lifecycle state, secrets, snapshots, or signing.
# INVARIANTS:
#   * No call ever uses ``shell=True`` — argv lists only, so no shell injection
#     is possible regardless of how user-provided strings flow through.
#   * Every call has a finite timeout; defaults to 60s, callers raise it.
#   * stderr from a failing call is preserved in the raised ``SandboxError``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from core.sandbox.models import (
    SandboxDockerUnavailable,
    SandboxError,
)

_DEFAULT_TIMEOUT_S = 60
_DOCKER_BIN_ENV = "AZTEA_SANDBOX_DOCKER_BIN"


def docker_binary() -> str:
    """Resolve the docker binary path; raise if it isn't on PATH.

    Why: a clear failure here beats a cryptic FileNotFoundError later.
    Test fixtures can override via ``AZTEA_SANDBOX_DOCKER_BIN``.
    """
    explicit = os.environ.get(_DOCKER_BIN_ENV)
    if explicit:
        if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
            raise SandboxDockerUnavailable(
                f"AZTEA_SANDBOX_DOCKER_BIN={explicit!r} is not executable"
            )
        return explicit
    found = shutil.which("docker")
    if not found:
        raise SandboxDockerUnavailable(
            "docker binary not found on PATH; install Docker or set "
            "AZTEA_SANDBOX_DOCKER_BIN"
        )
    return found


def docker_available() -> bool:
    """Pure-ish: ``True`` iff the docker daemon answers ``info`` quickly."""
    try:
        bin_path = docker_binary()
    except SandboxDockerUnavailable:
        return False
    try:
        proc = subprocess.run(  # noqa: S603
            [bin_path, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0 and bool(proc.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return False


def run_docker(
    argv: list[str],
    *,
    timeout: int = _DEFAULT_TIMEOUT_S,
    cwd: str | None = None,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker <argv>`` and return the completed process.

    Why: every Docker call funnels through here so timeout handling,
    error wrapping, and binary resolution stay single-sourced.
    """
    bin_path = docker_binary()
    full = [bin_path, *argv]
    try:
        proc = subprocess.run(  # noqa: S603
            full,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=_merged_env(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(
            f"docker call timed out after {timeout}s: {' '.join(argv[:3])}",
            details={"argv": argv[:8], "timeout_s": timeout},
        ) from exc
    if check and proc.returncode != 0:
        raise SandboxError(
            f"docker call failed (rc={proc.returncode}): {' '.join(argv[:3])}",
            details={
                "argv": argv[:8],
                "rc": proc.returncode,
                "stderr": (proc.stderr or "")[:1000],
            },
        )
    return proc


def run_compose(
    project_name: str,
    cwd: str,
    argv: list[str],
    *,
    files: list[str] | None = None,
    profiles: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: int = _DEFAULT_TIMEOUT_S,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose -p <project> [-f ...] [--profile ...] <argv>``.

    Why: the compose CLI is invoked from every lifecycle surface (start,
    logs, exec, db ops); centralising the argv assembly stops drift
    between call sites.
    """
    base = ["compose", "-p", project_name]
    for f in files or []:
        base.extend(["-f", f])
    for p in profiles or []:
        base.extend(["--profile", p])
    return run_docker(base + argv, timeout=timeout, cwd=cwd, env=env, check=check)


def _merged_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Pure: return host env merged with ``extra``; ``None`` keeps inheritance."""
    if not extra:
        return os.environ.copy()
    merged = os.environ.copy()
    merged.update({str(k): str(v) for k, v in extra.items()})
    return merged


def container_inspect(container: str) -> dict[str, Any] | None:
    """Return the ``docker inspect`` JSON for one container, or ``None`` if absent."""
    import json

    proc = run_docker(
        ["inspect", "--format", "{{json .}}", container],
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def list_project_containers(project_name: str) -> list[dict[str, Any]]:
    """Return the JSON-per-line container list for a compose project."""
    import json

    proc = run_docker(
        [
            "ps",
            "--all",
            "--format",
            "{{json .}}",
            "--filter",
            f"label=com.docker.compose.project={project_name}",
        ],
        timeout=15,
    )
    out: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
