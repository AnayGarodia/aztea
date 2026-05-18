"""sandbox_network_capture — privileged tcpdump sidecar on a sandbox network.

# OWNS: spawning a tcpdump container with --cap-add=NET_RAW joined to the
#       target sandbox's docker network; capturing the PCAP into the
#       per-sandbox state dir; reporting packet count + duration.
# NOT OWNS: any default-on privilege escalation. Operators MUST opt in via
#           ``AZTEA_SANDBOX_ALLOW_NET_RAW=1`` before this action becomes
#           callable; without the flag the action returns a structured
#           refusal explaining how to enable it.
"""

from __future__ import annotations

import logging
import os
import secrets
import shlex
from pathlib import Path
from typing import Any

from core.sandbox.docker_cli import run_docker
from core.sandbox.models import SandboxInvalidInput, SandboxNotFound
from core.sandbox.state import SandboxState, get, sandbox_dir

_LOG = logging.getLogger("aztea.sandbox.network_capture")
_NET_RAW_FLAG = "AZTEA_SANDBOX_ALLOW_NET_RAW"
_DEFAULT_DURATION_S = 30
_HARD_MAX_DURATION_S = 300
_DEFAULT_IMAGE = "nicolaka/netshoot:latest"
_PCAP_MAX_BYTES = 200 * 1024 * 1024  # 200 MB cap on the PCAP file


def network_capture(payload: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded tcpdump capture on a sandbox network and return the PCAP path.

    Why: the audit case for live_sandbox keeps surfacing "auth header
    isn't reaching upstream" — wire-level capture is the right
    primitive. We keep it explicitly opt-in so the default-deny posture
    on Docker capabilities stays intact for operators who don't need it.
    """
    state = _require(payload)
    if os.environ.get(_NET_RAW_FLAG, "") != "1":
        return _refused_envelope(state)
    duration = _validate_duration(payload.get("duration_seconds"))
    bpf_filter = str(payload.get("filter") or "").strip()
    if len(bpf_filter) > 512:
        raise SandboxInvalidInput("filter is limited to 512 chars")
    image = str(payload.get("image") or "").strip() or _DEFAULT_IMAGE
    network = state.boot.project_name + "_default"  # compose default net
    output_dir = sandbox_dir(state.sandbox_id) / "network_captures"
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pcap_name = f"capture_{secrets.token_hex(4)}.pcap"
    pcap_path = output_dir / pcap_name
    sidecar = f"aztea-pcap-{state.sandbox_id[-8:]}-{secrets.token_hex(3)}"
    argv = _build_sidecar_argv(
        sidecar=sidecar, image=image, network=network,
        pcap_path=pcap_path, duration=duration, bpf_filter=bpf_filter,
    )
    proc = run_docker(argv, timeout=duration + 30, check=False)
    if proc.returncode != 0:
        raise SandboxInvalidInput(
            f"tcpdump sidecar failed (rc={proc.returncode}): "
            f"{(proc.stderr or '')[:512]}"
        )
    size_bytes = pcap_path.stat().st_size if pcap_path.is_file() else 0
    if size_bytes > _PCAP_MAX_BYTES:
        pcap_path.unlink(missing_ok=True)
        raise SandboxInvalidInput(
            f"capture exceeded {_PCAP_MAX_BYTES // (1024*1024)} MB cap; "
            "tighten the BPF filter"
        )
    packet_count = _count_packets_with_tcpdump(image, sidecar, pcap_path)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "pcap_path": str(pcap_path),
        "pcap_size_bytes": size_bytes,
        "packet_count": packet_count,
        "duration_seconds": duration,
        "filter": bpf_filter,
        "network": network,
        "elevated": True,
        "note": (
            "Capture sidecar ran with NET_RAW (gated by "
            f"{_NET_RAW_FLAG}=1). The PCAP file is on the host "
            "filesystem; move it off-box before sandbox_stop."
        ),
    }


def _refused_envelope(state: SandboxState) -> dict[str, Any]:
    """Pure: structured refusal when NET_RAW gating is off.

    Why: never silently grant privileges — the response makes the
    operator opt-in explicit and explains how to flip it on.
    """
    return {
        "sandbox_id": state.sandbox_id,
        "elevated": False,
        "refused": True,
        "reason": (
            f"network_capture requires NET_RAW which is gated behind "
            f"{_NET_RAW_FLAG}=1 in the server environment. Set it on "
            "the host that runs the Aztea server (e.g. add "
            f"{_NET_RAW_FLAG}=1 to .env), then restart, then retry."
        ),
        "next_step": (
            f"export {_NET_RAW_FLAG}=1 && systemctl restart aztea-server "
            "  # or: launchctl unload/load on macOS"
        ),
    }


def _build_sidecar_argv(
    *,
    sidecar: str,
    image: str,
    network: str,
    pcap_path: Path,
    duration: int,
    bpf_filter: str,
) -> list[str]:
    """Pure: argv for the privileged tcpdump sidecar."""
    inside_pcap = "/captures/out.pcap"
    bind_dir = str(pcap_path.parent)
    cmd = ["timeout", str(duration), "tcpdump", "-i", "any", "-U", "-w", inside_pcap]
    if bpf_filter:
        cmd.extend(shlex.split(bpf_filter))
    return [
        "run",
        "--rm",
        "--name", sidecar,
        "--network", network,
        "--cap-add=NET_RAW",
        "--cap-add=NET_ADMIN",
        "-v", f"{bind_dir}:/captures",
        image,
        *cmd,
    ]


def _count_packets_with_tcpdump(image: str, sidecar: str, pcap_path: Path) -> int | None:
    """Side-effect: run ``tcpdump -r <pcap> -c 0 -nn`` to count packets.

    Why: pcap inspection lives in the same sidecar image we already
    pulled. Keeping it inline avoids forcing an additional dep on the
    host (libpcap, scapy, etc.).
    """
    bind_dir = str(pcap_path.parent)
    inside = "/captures/" + pcap_path.name
    proc = run_docker(
        [
            "run", "--rm",
            "-v", f"{bind_dir}:/captures",
            image,
            "sh", "-lc", f"tcpdump -r {inside} 2>/dev/null | wc -l",
        ],
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return int((proc.stdout or "").strip())
    except ValueError:
        return None


def _validate_duration(value: Any) -> int:
    """Pure: clamp the capture duration to a safe range."""
    try:
        seconds = int(value) if value is not None else _DEFAULT_DURATION_S
    except (TypeError, ValueError) as exc:
        raise SandboxInvalidInput("duration_seconds must be an integer") from exc
    if seconds <= 0:
        raise SandboxInvalidInput("duration_seconds must be > 0")
    if seconds > _HARD_MAX_DURATION_S:
        raise SandboxInvalidInput(
            f"duration_seconds capped at {_HARD_MAX_DURATION_S} ({_HARD_MAX_DURATION_S//60} min)"
        )
    return seconds


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state
