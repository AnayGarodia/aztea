"""Boot-strategy detection + execution.

# OWNS: ``auto`` / ``docker_compose`` / ``dockerfile`` / ``devcontainer`` /
#       ``custom_commands`` boot paths and the ``ready_checks`` loop.
# NOT OWNS: source materialisation (see source.py), per-action exec or DB ops.
# INVARIANTS:
#   * Boot always produces a populated ``BootInfo`` (project_name, strategy,
#     services map) — caller-facing failure surfaces as ``SandboxBootFailed``.
#   * ``ready_checks`` honour the spec's four kinds: ``http``, ``tcp``,
#     ``log_regex``, ``command``.
"""

from __future__ import annotations

import json
import logging
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from core.sandbox.docker_cli import (
    container_inspect,
    list_project_containers,
    run_compose,
    run_docker,
)
from core.sandbox.models import (
    DEFAULT_READY_TIMEOUT_S,
    SandboxBootFailed,
    SandboxInvalidInput,
)
from core.sandbox.state import BootInfo, project_name_for

_LOG = logging.getLogger("aztea.sandbox.boot")
_COMPOSE_FILE_CANDIDATES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    "docker-compose.dev.yml",
)
_DOCKERFILE_CANDIDATES = ("Dockerfile", "dockerfile")
_DEVCONTAINER_PATHS = (".devcontainer/devcontainer.json", "devcontainer.json")
_POSTGRES_IMAGE_HINTS = ("postgres", "pgvector", "timescale")
_POSTGRES_SERVICE_HINTS = ("db", "postgres", "pg")


def detect_strategy(repo_path: str) -> str:
    """Pure-ish: pick the best strategy for a freshly-cloned repo.

    Why: ``auto`` is the default — without an explicit choice we look for
    the most reliable signal in this order: compose > devcontainer >
    dockerfile > custom (the caller must supply commands for the last).
    """
    p = Path(repo_path)
    for candidate in _COMPOSE_FILE_CANDIDATES:
        if (p / candidate).is_file():
            return "docker_compose"
    for candidate in _DEVCONTAINER_PATHS:
        if (p / candidate).is_file():
            return "devcontainer"
    for candidate in _DOCKERFILE_CANDIDATES:
        if (p / candidate).is_file():
            return "dockerfile"
    return "custom_commands"


def boot(
    *,
    sandbox_id: str,
    repo_path: str,
    boot_cfg: dict[str, Any],
    env_vars: dict[str, str],
    network_argv: list[str],
    project_name_override: str | None = None,
) -> BootInfo:
    """Dispatch the chosen boot strategy and return populated ``BootInfo``.

    ``boot_cfg`` is the caller's ``boot`` block (strategy + per-strategy fields).
    """
    project = project_name_override or project_name_for(sandbox_id)
    strategy = str(boot_cfg.get("strategy") or "auto").strip()
    if strategy == "auto":
        strategy = detect_strategy(repo_path)
    if strategy == "docker_compose":
        info = _boot_docker_compose(project, repo_path, boot_cfg, env_vars)
    elif strategy == "dockerfile":
        info = _boot_dockerfile(project, repo_path, boot_cfg, env_vars, network_argv)
    elif strategy == "devcontainer":
        info = _boot_devcontainer(project, repo_path, boot_cfg, env_vars, network_argv)
    elif strategy == "custom_commands":
        info = _boot_custom(project, repo_path, boot_cfg, env_vars, network_argv)
    else:
        raise SandboxInvalidInput(f"unsupported boot.strategy: {strategy!r}")
    _wait_for_ready(info, boot_cfg, repo_path)
    return info


def _boot_docker_compose(
    project: str, repo_path: str, boot_cfg: dict[str, Any], env_vars: dict[str, str]
) -> BootInfo:
    files = list(boot_cfg.get("compose_files") or [])
    if not files:
        files = [f for f in _COMPOSE_FILE_CANDIDATES if (Path(repo_path) / f).is_file()][:1]
    if not files:
        raise SandboxBootFailed("no docker compose file found in repo root")
    profiles = list(boot_cfg.get("compose_profiles") or [])
    t0 = time.time()
    run_compose(
        project,
        repo_path,
        ["up", "--build", "--detach", "--remove-orphans"],
        files=files,
        profiles=profiles,
        env=env_vars,
        timeout=900,
    )
    build_up = round(time.time() - t0, 2)
    services = _collect_compose_services(project)
    info = BootInfo(
        strategy="docker_compose",
        project_name=project,
        services=services,
        boot_timing={"build_up": build_up},
    )
    _attach_postgres_metadata(info, services)
    return info


def _boot_dockerfile(
    project: str,
    repo_path: str,
    boot_cfg: dict[str, Any],
    env_vars: dict[str, str],
    network_argv: list[str],
) -> BootInfo:
    image_tag = f"aztea-{project}:latest"
    run_docker(
        ["build", "-t", image_tag, "."],
        cwd=repo_path,
        timeout=900,
    )
    container_name = f"{project}-app"
    argv = [
        "run",
        "--detach",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        f"aztea.sandbox.project={project}",
    ]
    argv.extend(network_argv)
    for key, val in env_vars.items():
        argv.extend(["-e", f"{key}={val}"])
    argv.append(image_tag)
    custom = boot_cfg.get("custom_commands") or []
    if custom:
        argv.extend(["sh", "-lc", " && ".join(str(c) for c in custom)])
    run_docker(argv, timeout=120)
    services = {
        "app": {
            "container": container_name,
            "image": image_tag,
            "ports": _port_map(container_name),
        }
    }
    return BootInfo(
        strategy="dockerfile",
        project_name=project,
        services=services,
        boot_timing={"build_up": 0.0},
    )


def _boot_devcontainer(
    project: str,
    repo_path: str,
    boot_cfg: dict[str, Any],
    env_vars: dict[str, str],
    network_argv: list[str],
) -> BootInfo:
    """Side-effect: launch the devcontainer.json image with the workspace bind-mounted.

    Why: a minimal devcontainer boot is enough for most popular templates
    (image + workspaceFolder). features / postCreateCommand / dockerComposeFile
    fan-out is documented in the README as a follow-up issue.
    """
    raw = None
    for path in _DEVCONTAINER_PATHS:
        candidate = Path(repo_path) / path
        if candidate.is_file():
            raw = candidate.read_text("utf-8")
            break
    if raw is None:
        raise SandboxBootFailed("devcontainer.json not found")
    try:
        cfg = json.loads(raw)
    except ValueError as exc:
        raise SandboxBootFailed(f"devcontainer.json is not valid JSON: {exc}") from exc
    image = cfg.get("image") or cfg.get("dockerFile")
    if not isinstance(image, str) or not image.strip():
        raise SandboxBootFailed(
            "devcontainer.json must declare a top-level 'image' (composite "
            "devcontainer variants are tracked in follow-up issue)"
        )
    workspace_folder = cfg.get("workspaceFolder") or "/workspace"
    container_name = f"{project}-devcontainer"
    argv = [
        "run",
        "--detach",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        f"aztea.sandbox.project={project}",
        "-v",
        f"{repo_path}:{workspace_folder}",
        "-w",
        workspace_folder,
    ]
    argv.extend(network_argv)
    for key, val in env_vars.items():
        argv.extend(["-e", f"{key}={val}"])
    argv.extend([image, "sleep", "infinity"])
    run_docker(argv, timeout=120)
    return BootInfo(
        strategy="devcontainer",
        project_name=project,
        services={"app": {"container": container_name, "image": image}},
        boot_timing={"build_up": 0.0},
    )


def _boot_custom(
    project: str,
    repo_path: str,
    boot_cfg: dict[str, Any],
    env_vars: dict[str, str],
    network_argv: list[str],
) -> BootInfo:
    """Side-effect: run the supplied ``custom_commands`` inside a generic image.

    Why: a degraded path that lets users with no Dockerfile still get a
    sandbox — the engine boots an Ubuntu container with their workspace
    bind-mounted and runs the script there.
    """
    cmds = list(boot_cfg.get("custom_commands") or [])
    if not cmds:
        raise SandboxInvalidInput("custom_commands requires boot.custom_commands list")
    base_image = str(boot_cfg.get("base_image") or "ubuntu:22.04")
    container_name = f"{project}-custom"
    argv = [
        "run",
        "--detach",
        "--rm",
        "--name",
        container_name,
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        f"aztea.sandbox.project={project}",
        "-v",
        f"{repo_path}:/repo",
        "-w",
        "/repo",
    ]
    argv.extend(network_argv)
    for key, val in env_vars.items():
        argv.extend(["-e", f"{key}={val}"])
    script = " && ".join(str(c) for c in cmds)
    argv.extend([base_image, "bash", "-lc", f"{script} && tail -f /dev/null"])
    run_docker(argv, timeout=300)
    return BootInfo(
        strategy="custom_commands",
        project_name=project,
        services={
            "app": {"container": container_name, "image": base_image}
        },
        boot_timing={"build_up": 0.0},
    )


def _collect_compose_services(project: str) -> dict[str, dict[str, Any]]:
    """Side-effect: enumerate compose containers by ``docker ps`` filter."""
    services: dict[str, dict[str, Any]] = {}
    for entry in list_project_containers(project):
        service = (
            entry.get("Service")
            or entry.get("Label", "")
            or entry.get("Names", "")
        )
        if not service:
            continue
        container = entry.get("Names") or entry.get("ID") or service
        services[service] = {
            "container": container,
            "image": entry.get("Image"),
            "state": entry.get("State"),
            "ports": entry.get("Publishers") or _port_map(container),
        }
    return services


def _port_map(container: str) -> list[dict[str, Any]]:
    """Side-effect: ``docker inspect`` parsed into a small published-port list."""
    info = container_inspect(container)
    if not info:
        return []
    ports = ((info.get("NetworkSettings") or {}).get("Ports") or {})
    out: list[dict[str, Any]] = []
    for key, value in ports.items():
        if not value:
            continue
        for binding in value:
            out.append(
                {
                    "internal_port": key,
                    "host_ip": binding.get("HostIp"),
                    "host_port": binding.get("HostPort"),
                }
            )
    return out


def _attach_postgres_metadata(info: BootInfo, services: dict[str, dict[str, Any]]) -> None:
    """Pure-ish: heuristic Postgres detection for the DB surface."""
    for name, meta in services.items():
        image = str(meta.get("image") or "").lower()
        if any(hint in image for hint in _POSTGRES_IMAGE_HINTS) or any(
            hint == name.lower() for hint in _POSTGRES_SERVICE_HINTS
        ):
            info.detected_postgres_service = name
            env = _container_env(meta.get("container") or "")
            info.detected_postgres_db = env.get("POSTGRES_DB", "postgres")
            info.detected_postgres_user = env.get("POSTGRES_USER", "postgres")
            return


def _container_env(container: str) -> dict[str, str]:
    """Side-effect: read container env from ``docker inspect``."""
    info = container_inspect(container)
    if not info:
        return {}
    env_pairs = ((info.get("Config") or {}).get("Env") or [])
    out: dict[str, str] = {}
    for entry in env_pairs:
        if "=" in entry:
            k, v = entry.split("=", 1)
            out[k] = v
    return out


def _wait_for_ready(info: BootInfo, boot_cfg: dict[str, Any], repo_path: str) -> None:
    """Side-effect: poll user-supplied ``ready_checks`` until they pass or time out."""
    checks = list(boot_cfg.get("ready_checks") or [])
    if not checks:
        return
    timeout = int(boot_cfg.get("ready_timeout_seconds") or DEFAULT_READY_TIMEOUT_S)
    deadline = time.time() + timeout
    t0 = time.time()
    while time.time() < deadline:
        statuses = [_run_ready_check(info, check, repo_path) for check in checks]
        if all(statuses):
            info.boot_timing["ready"] = round(time.time() - t0, 2)
            return
        time.sleep(1.5)
    failed = [
        check for check, ok in zip(checks, statuses, strict=False) if not ok
    ]
    raise SandboxBootFailed(
        f"ready_checks did not pass within {timeout}s",
        details={"unsatisfied": failed[:5]},
    )


def _run_ready_check(info: BootInfo, check: dict[str, Any], repo_path: str) -> bool:
    """Pure-ish: evaluate one ready check; ``True`` means satisfied."""
    kind = str((check or {}).get("kind") or "").strip().lower()
    target = (check or {}).get("target")
    if kind == "http":
        return _check_http(str(target or ""), int((check or {}).get("expect_status") or 200))
    if kind == "tcp":
        return _check_tcp(str(target or ""))
    if kind == "log_regex":
        return _check_log_regex(info, check)
    if kind == "command":
        return _check_command(info, repo_path, check)
    return False


def _check_http(url: str, expect_status: int) -> bool:
    try:
        req = urllib.request.Request(  # noqa: S310 (sandbox internal URL)
            url, headers={"User-Agent": "aztea-live-sandbox/1"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status == expect_status
    except urllib.error.HTTPError as exc:
        return exc.code == expect_status
    except Exception:
        return False


def _check_tcp(target: str) -> bool:
    if ":" not in target:
        return False
    host, port_s = target.rsplit(":", 1)
    try:
        port = int(port_s)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _check_log_regex(info: BootInfo, check: dict[str, Any]) -> bool:
    import re

    service = str(check.get("service") or "")
    pattern = str(check.get("pattern") or "")
    if not service or not pattern:
        return False
    meta = info.services.get(service)
    if not meta:
        return False
    container = meta.get("container") or service
    proc = run_docker(["logs", "--tail", "500", container], timeout=15, check=False)
    if proc.returncode != 0:
        return False
    return bool(re.search(pattern, proc.stdout + proc.stderr, re.MULTILINE))


def _check_command(info: BootInfo, repo_path: str, check: dict[str, Any]) -> bool:
    cmd = str(check.get("cmd") or "")
    if not cmd:
        return False
    service = str(check.get("service") or "")
    if service and info.services.get(service):
        container = info.services[service].get("container") or service
        proc = run_docker(
            ["exec", container, "sh", "-c", cmd], timeout=30, check=False
        )
    else:
        proc = subprocess.run(  # noqa: S603
            shlex.split(cmd),
            capture_output=True,
            text=True,
            cwd=repo_path,
            timeout=30,
        )
    return proc.returncode == 0
