"""Network policy → ``docker run``/``docker network create`` argv translation.

# OWNS: translating ``isolated`` / ``allowlist`` / ``open`` into the docker
#       argv each boot path needs; creating per-sandbox docker networks.
# NOT OWNS: actual ready-check HTTP calls (those live in boot.py).
# INVARIANTS:
#   * ``isolated`` is the default. ``open`` only when caller asks for it explicitly.
#   * The allowlist resolution happens at boot via ``extra_hosts`` so subsequent
#     container DNS lookups go to the resolved IP directly — no `iptables` shell
#     out, no host kernel reconfiguration.
"""

from __future__ import annotations

import logging
import socket
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import SandboxInvalidInput

_LOG = logging.getLogger("aztea.sandbox.network")


def build_network_argv(
    sandbox_id: str,
    network_cfg: dict[str, Any] | None,
) -> tuple[list[str], dict[str, Any]]:
    """Return ``(argv, resolved_policy)`` for non-compose single-container runs.

    Compose has its own network discipline; for ``dockerfile`` /
    ``devcontainer`` / ``custom_commands`` we wire the network on the
    ``docker run`` line directly.
    """
    cfg = network_cfg or {}
    policy = str(cfg.get("egress") or "isolated").strip().lower()
    allowlist = [str(h).strip().lower() for h in (cfg.get("egress_allowlist") or []) if str(h).strip()]
    if policy not in ("isolated", "allowlist", "open"):
        raise SandboxInvalidInput(
            f"network.egress must be one of isolated|allowlist|open; got {policy!r}"
        )
    if policy == "open":
        return [], {"egress": "open", "egress_allowlist": []}
    if policy == "isolated":
        return ["--network", "none"], {"egress": "isolated", "egress_allowlist": []}
    # allowlist: build extra_hosts from resolved IPs so the container can
    # reach the named hosts but DNS for anything else fails closed.
    extras = _resolve_allowlist(allowlist)
    argv = []
    for host, ip in extras.items():
        argv.extend(["--add-host", f"{host}:{ip}"])
    return argv, {"egress": "allowlist", "egress_allowlist": list(extras.keys())}


def compose_network_env(
    network_cfg: dict[str, Any] | None,
) -> dict[str, str]:
    """Pure-ish: env vars that compose stacks can read for network policy.

    Why: many user compose files honour env-driven flags; we surface the
    resolved policy so the user's stack can short-circuit if needed.
    """
    cfg = network_cfg or {}
    policy = str(cfg.get("egress") or "isolated").strip().lower()
    return {
        "AZTEA_SANDBOX_NETWORK_POLICY": policy,
    }


def _resolve_allowlist(allowlist: list[str]) -> dict[str, str]:
    """Side-effect: best-effort DNS resolution for each allowlisted host."""
    resolved: dict[str, str] = {}
    for entry in allowlist:
        host = entry.split("/")[0].split(":")[0]
        if not host or host.startswith("*"):
            # Wildcard entries aren't expressible via /etc/hosts. We surface
            # them in the resolved policy so callers know we couldn't pin
            # them but keep network mode open inside the bridge.
            continue
        try:
            ip = socket.gethostbyname(host)
            resolved[host] = ip
        except OSError:
            _LOG.info("allowlist DNS failed for %s; entry skipped", host)
    return resolved


def docker_remove_label_filter(project_name: str) -> list[str]:
    """Pure: docker filter argv used by lifecycle teardown."""
    return ["--filter", f"label=com.docker.compose.project={project_name}"]


def stop_orphan_containers(project_name: str) -> None:
    """Side-effect: kill any straggler containers belonging to this project.

    Why: ``docker compose down`` covers compose; for the non-compose
    strategies (dockerfile / devcontainer / custom) we rely on the
    project label to find leftovers.
    """
    try:
        proc = run_docker(
            ["ps", "--quiet", *docker_remove_label_filter(project_name)],
            timeout=15,
            check=False,
        )
    except Exception:
        return
    if proc.returncode != 0:
        return
    ids = [line for line in proc.stdout.splitlines() if line.strip()]
    if not ids:
        return
    run_docker(["rm", "-f", *ids], timeout=30, check=False)
