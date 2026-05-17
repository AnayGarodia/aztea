"""sandbox_tunnel_open / _close — expose a sandbox service to the public web.

# OWNS: process state for each open tunnel, which tunneling tool is used,
#       and the per-tunnel cleanup on close.
# NOT OWNS: TLS termination or auth gating — those are properties of the
#           tunneling tool itself. For cloudflared quick tunnels the
#           public URL is TLS-terminated by Cloudflare automatically.
# INVARIANTS:
#   * One open tunnel per (sandbox_id, service, port). Re-opening returns
#     the cached entry.
#   * tunnel_open detects cloudflared first (no account needed for quick
#     tunnels), then ngrok (requires AZTEA_NGROK_TOKEN), then degrades
#     to a host-bound port (localhost-only, but functional for local
#     end-to-end demos).
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Any

from core.sandbox.docker_cli import container_inspect
from core.sandbox.models import SandboxInvalidInput, SandboxNotFound, SandboxServiceMissing
from core.sandbox.state import SandboxState, get

_LOG = logging.getLogger("aztea.sandbox.tunnels")

_CLOUDFLARED_URL_RE = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
_NGROK_URL_RE = re.compile(r"(https://[a-z0-9-]+\.ngrok-free\.app|https://[a-z0-9-]+\.ngrok\.io)")
_TUNNEL_BOOT_TIMEOUT_S = 25
_TUNNEL_POLL_INTERVAL_S = 0.5
# Substrings that signal a Cloudflare rate-limit / quota error in the
# cloudflared CLI output. Surfaces back to the caller as a structured
# refusal so they know to switch to a named tunnel.
_CLOUDFLARED_RATE_LIMIT_HINTS = (
    "429",
    "Too many requests",
    "rate limit",
    "exceeded",
)
_NAMED_TUNNEL_TOKEN_ENV = "AZTEA_CLOUDFLARE_TUNNEL_TOKEN"

# (sandbox_id, tunnel_id) → {Popen, kind, public_url, service, port, created_at}
_TUNNELS: dict[str, dict[str, Any]] = {}
_TUNNELS_LOCK = threading.RLock()


def tunnel_open(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose a service:port from this sandbox over a public URL.

    Why: the spec needs public tunnels for OAuth callbacks, Stripe
    webhooks, and "share this with my teammate" flows. We try
    cloudflared first because its quick-tunnel mode needs no account
    or token — install cloudflared on the host and it just works. Fall
    back to ngrok when the user has supplied a token. Fall back to a
    host-bound port when neither is available so the URL is at least
    reachable from the host loopback.
    """
    state, service, port = _validate_tunnel_input(payload)
    auth = str(payload.get("auth") or "none").strip().lower()
    if auth not in ("bearer", "none"):
        raise SandboxInvalidInput("auth must be 'bearer' or 'none'")
    hostname_hint = str(payload.get("hostname_hint") or "").strip().lower()
    host_port = _resolve_host_port(state, service, port)
    if host_port is None:
        raise SandboxInvalidInput(
            f"service '{service}' is not publishing port {port} to the host; "
            "add a 'ports:' entry to its compose service so the tunnel "
            "has a host-side port to forward"
        )
    existing = _find_existing(state.sandbox_id, service, port)
    if existing is not None:
        state.touch()
        return _serialise(existing)
    tunnel = _open_with_best_available_tool(host_port, hostname_hint)
    tunnel_id = f"tun_{secrets.token_hex(6)}"
    record = {
        "tunnel_id": tunnel_id,
        "sandbox_id": state.sandbox_id,
        "service": service,
        "port": port,
        "host_port": host_port,
        "auth": auth,
        "auth_token": secrets.token_urlsafe(24) if auth == "bearer" else None,
        "kind": tunnel["kind"],
        "public_url": tunnel["public_url"],
        "process": tunnel.get("process"),
        "created_at": int(time.time()),
        "expires_at": state.expires_at,
        "note": tunnel.get("note"),
    }
    with _TUNNELS_LOCK:
        _TUNNELS[tunnel_id] = record
    state.touch()
    return _serialise(record)


def tunnel_close(payload: dict[str, Any]) -> dict[str, Any]:
    """Tear down a tunnel by id; safe to call after the tool has already exited."""
    state = _require(payload)
    tunnel_id = str(payload.get("tunnel_id") or "").strip()
    if not tunnel_id:
        raise SandboxInvalidInput("tunnel_id is required")
    with _TUNNELS_LOCK:
        record = _TUNNELS.pop(tunnel_id, None)
    if record is None or record.get("sandbox_id") != state.sandbox_id:
        raise SandboxNotFound(
            f"tunnel '{tunnel_id}' not found for sandbox '{state.sandbox_id}'"
        )
    _terminate_tunnel(record)
    state.touch()
    return {
        "sandbox_id": state.sandbox_id,
        "tunnel_id": tunnel_id,
        "closed": True,
        "kind": record.get("kind"),
    }


def list_open_tunnels(sandbox_id: str) -> list[dict[str, Any]]:
    """Pure: snapshot of open tunnels for ``sandbox_id``."""
    return [_serialise(r) for r in _TUNNELS.values() if r.get("sandbox_id") == sandbox_id]


def evict_for_sandbox(sandbox_id: str) -> int:
    """Side-effect: close every tunnel belonging to ``sandbox_id``.

    Why: ``lifecycle.stop`` calls this so a sandbox teardown also kills
    the tunnel processes — without this an orphaned cloudflared keeps
    publishing a now-dead service URL.
    """
    closed = 0
    with _TUNNELS_LOCK:
        ids = [tid for tid, r in _TUNNELS.items() if r.get("sandbox_id") == sandbox_id]
        for tid in ids:
            record = _TUNNELS.pop(tid, None)
            if record is not None:
                _terminate_tunnel(record)
                closed += 1
    return closed


def _open_with_best_available_tool(host_port: int, hostname_hint: str) -> dict[str, Any]:
    """Side-effect: launch the actual tunneling subprocess; return its state.

    Selection order:
      1. cloudflared + AZTEA_CLOUDFLARE_TUNNEL_TOKEN  → production-grade
         named tunnel (no rate limit, stable hostname per account).
      2. cloudflared → quick tunnel (free, rate-limited per IP).
      3. ngrok + AZTEA_NGROK_TOKEN → ngrok-managed tunnel.
      4. Degraded localhost URL (always available, never public).
    """
    import os

    cloudflared_path = shutil.which("cloudflared")
    if cloudflared_path and os.environ.get(_NAMED_TUNNEL_TOKEN_ENV):
        return _open_cloudflared_named(host_port, hostname_hint)
    if cloudflared_path:
        return _open_cloudflared_quick(host_port, hostname_hint)
    if shutil.which("ngrok") and os.environ.get("AZTEA_NGROK_TOKEN"):
        return _open_ngrok(host_port, hostname_hint)
    return _degraded_local_tunnel(host_port)


def _open_cloudflared_quick(host_port: int, hostname_hint: str) -> dict[str, Any]:
    """Side-effect: spawn ``cloudflared tunnel --url http://localhost:<port>`` (quick).

    Quick tunnels are rate-limited per host IP — we surface a structured
    refusal when cloudflared reports the rate limit so the caller knows
    to configure a named tunnel via AZTEA_CLOUDFLARE_TUNNEL_TOKEN.
    """
    proc = subprocess.Popen(  # noqa: S603
        [
            "cloudflared", "tunnel",
            "--url", f"http://localhost:{host_port}",
            "--no-autoupdate",
            "--metrics", "127.0.0.1:0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = _wait_for_url_or_rate_limit(
        proc, _CLOUDFLARED_URL_RE, "cloudflared quick tunnel",
    )
    return {
        "kind": "cloudflared_quick",
        "public_url": url,
        "process": proc,
        "note": (
            "Quick tunnel — Cloudflare rate-limits these per host IP "
            "(roughly a handful per hour). For production set "
            f"{_NAMED_TUNNEL_TOKEN_ENV} in the server env to use a "
            "named tunnel against your Cloudflare account."
        ),
    }


def _open_cloudflared_named(host_port: int, hostname_hint: str) -> dict[str, Any]:
    """Side-effect: spawn a named cloudflared tunnel via the configured token.

    Why: named tunnels are not rate-limited, expose a stable hostname per
    account, and authenticate at Cloudflare's edge. The token holds the
    routing config; we just run ``cloudflared tunnel run --token <T>``
    pointed at the local host port.
    """
    import os

    token = os.environ.get(_NAMED_TUNNEL_TOKEN_ENV, "")
    proc = subprocess.Popen(  # noqa: S603
        [
            "cloudflared", "tunnel", "run",
            "--token", token,
            "--no-autoupdate",
            "--url", f"http://localhost:{host_port}",
            "--metrics", "127.0.0.1:0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Named tunnels emit the configured hostname via their dashboard
    # rather than stdout. We block briefly to confirm cloudflared came up
    # cleanly, then return the configured hostname if it appears, or a
    # generic "managed" URL hint otherwise.
    detected_url = _wait_for_url_or_rate_limit(
        proc, _CLOUDFLARED_URL_RE, "cloudflared named tunnel",
        accept_no_url=True,
    )
    return {
        "kind": "cloudflared_named",
        "public_url": detected_url or "managed:cloudflare-named-tunnel",
        "process": proc,
        "note": (
            "Named tunnel running. Public hostname is whatever's configured "
            "in your Cloudflare Zero Trust dashboard for this tunnel "
            "token; the local cloudflared CLI doesn't echo it."
        ),
    }


def _open_ngrok(host_port: int, hostname_hint: str) -> dict[str, Any]:
    """Side-effect: spawn ``ngrok http <port>`` with the configured authtoken."""
    proc = subprocess.Popen(  # noqa: S603
        ["ngrok", "http", str(host_port), "--log=stdout", "--log-format=json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = _wait_for_url(proc, _NGROK_URL_RE, "ngrok")
    return {
        "kind": "ngrok",
        "public_url": url,
        "process": proc,
        "note": "ngrok tunnel — billed against your AZTEA_NGROK_TOKEN.",
    }


def _degraded_local_tunnel(host_port: int) -> dict[str, Any]:
    """Pure: return a localhost URL when no public-tunnel tool is installed.

    Why: still useful for local end-to-end demos (Stripe CLI forwarding,
    a teammate on the same machine). The returned URL is NOT publicly
    reachable; the response makes that explicit so callers don't expect
    inbound webhooks to land here.
    """
    return {
        "kind": "local",
        "public_url": f"http://localhost:{host_port}",
        "process": None,
        "note": (
            "No public tunneling tool detected on the host. Returned URL "
            "is localhost-only. Install cloudflared (apt-get install "
            "cloudflared) for a public quick tunnel, or set "
            "AZTEA_NGROK_TOKEN + install ngrok."
        ),
    }


def _wait_for_url_or_rate_limit(
    proc: subprocess.Popen[str],
    pattern: re.Pattern[str],
    kind: str,
    *,
    accept_no_url: bool = False,
) -> str | None:
    """Read tunneling subprocess stdout until URL appears or a rate-limit error fires.

    Why: cloudflared quick tunnels emit a Cloudflare error message on
    rate limit instead of a URL. Pre-fix we just timed out with a
    generic message; now we surface the specific cause so the caller
    knows to switch to a named tunnel. ``accept_no_url`` lets the named-
    tunnel path return None gracefully — the tunnel can run cleanly
    without ever printing a trycloudflare URL.
    """
    deadline = time.time() + _TUNNEL_BOOT_TIMEOUT_S
    buffer: list[str] = []
    assert proc.stdout is not None
    while time.time() < deadline:
        if proc.poll() is not None:
            joined = "".join(buffer)[:1024]
            raise SandboxInvalidInput(
                f"{kind} exited before publishing a URL "
                f"(rc={proc.returncode}); stdout tail: {joined}"
            )
        line = proc.stdout.readline()
        if not line:
            time.sleep(_TUNNEL_POLL_INTERVAL_S)
            continue
        buffer.append(line)
        match = pattern.search(line)
        if match:
            threading.Thread(
                target=_drain_pipe, args=(proc,), daemon=True,
            ).start()
            return match.group(1)
        if _looks_rate_limited(line):
            _terminate_process(proc)
            raise SandboxInvalidInput(
                f"{kind} was rate-limited by Cloudflare. Quick tunnels "
                f"are throttled per host IP. Set {_NAMED_TUNNEL_TOKEN_ENV} "
                "in the server env to use a named tunnel against your "
                "Cloudflare account instead. cloudflared said: "
                f"{line.strip()[:200]}"
            )
    if accept_no_url:
        threading.Thread(
            target=_drain_pipe, args=(proc,), daemon=True,
        ).start()
        return None
    _terminate_process(proc)
    raise SandboxInvalidInput(
        f"{kind} did not publish a URL within {_TUNNEL_BOOT_TIMEOUT_S}s"
    )


def _looks_rate_limited(line: str) -> bool:
    """Pure: True iff a line looks like a Cloudflare rate-limit error."""
    lowered = line.lower()
    return any(hint.lower() in lowered for hint in _CLOUDFLARED_RATE_LIMIT_HINTS)


# Back-compat shim so the rest of the module's call sites still resolve.
def _wait_for_url(
    proc: subprocess.Popen[str], pattern: re.Pattern[str], kind: str,
) -> str:
    """Pre-fix call-site shim: now delegates to the rate-limit-aware variant."""
    result = _wait_for_url_or_rate_limit(proc, pattern, kind)
    assert result is not None, "non-accept_no_url path must return a URL"
    return result


def _drain_pipe(proc: subprocess.Popen[str]) -> None:
    """Side-effect: keep reading stdout so the pipe doesn't fill up."""
    if proc.stdout is None:
        return
    try:
        for _ in iter(proc.stdout.readline, ""):
            if proc.poll() is not None:
                return
    except Exception:
        _LOG.debug("drain_pipe error", exc_info=True)


def _terminate_tunnel(record: dict[str, Any]) -> None:
    """Side-effect: kill the tunneling subprocess if one is attached."""
    proc = record.get("process")
    if proc is None:
        return
    _terminate_process(proc)


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    """Side-effect: SIGTERM, wait briefly, then SIGKILL if needed."""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
    except Exception:
        _LOG.debug("tunnel process termination failed", exc_info=True)


def _resolve_host_port(state: SandboxState, service: str, port: int) -> int | None:
    """Pure-ish: find the host-side port that maps to ``container:port``."""
    meta = state.boot.services.get(service)
    if meta is None:
        return None
    ports = meta.get("ports") or []
    # Compose `ports` populated by boot._port_map is a list of dicts:
    # {internal_port: "3000/tcp", host_ip: "0.0.0.0", host_port: "12345"}
    for entry in ports:
        if not isinstance(entry, dict):
            continue
        internal = str(entry.get("internal_port") or "").split("/", 1)[0]
        if internal == str(port):
            host_port = entry.get("host_port")
            if host_port:
                return int(host_port)
    # Final fallback: query docker inspect directly so a service that was
    # published after the initial boot still resolves. Swallow Docker
    # unavailability — the in-memory ports map is the source of truth
    # in that case and we already exhausted it above.
    try:
        info = container_inspect(meta.get("container") or service)
    except Exception:  # noqa: BLE001 — degrade gracefully
        info = None
    if info:
        nets = (info.get("NetworkSettings") or {}).get("Ports") or {}
        key = f"{port}/tcp"
        for binding in nets.get(key) or []:
            if isinstance(binding, dict) and binding.get("HostPort"):
                return int(binding["HostPort"])
    return None


def _find_existing(sandbox_id: str, service: str, port: int) -> dict[str, Any] | None:
    """Pure: return an existing tunnel record for the same triple, else None."""
    for record in _TUNNELS.values():
        if (
            record.get("sandbox_id") == sandbox_id
            and record.get("service") == service
            and record.get("port") == port
        ):
            return record
    return None


def _validate_tunnel_input(
    payload: dict[str, Any],
) -> tuple[SandboxState, str, int]:
    state = _require(payload)
    service = str(payload.get("service") or "").strip()
    if not service:
        raise SandboxInvalidInput("service is required")
    if service not in state.boot.services:
        raise SandboxServiceMissing(
            f"service '{service}' not found; available: {sorted(state.boot.services)}"
        )
    try:
        port = int(payload.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise SandboxInvalidInput("port must be a positive integer") from exc
    if not 1 <= port <= 65_535:
        raise SandboxInvalidInput("port must be in 1..65535")
    return state, service, port


def _serialise(record: dict[str, Any]) -> dict[str, Any]:
    """Pure: shape the public tunnel record (process handle stripped)."""
    return {
        "sandbox_id": record["sandbox_id"],
        "tunnel_id": record["tunnel_id"],
        "service": record["service"],
        "port": record["port"],
        "host_port": record["host_port"],
        "kind": record["kind"],
        "public_url": record["public_url"],
        "auth": record["auth"],
        "auth_token": record["auth_token"],
        "expires_at": record.get("expires_at"),
        "created_at": record["created_at"],
        "note": record.get("note"),
    }


def _require(payload: dict[str, Any]) -> SandboxState:
    sandbox_id = str((payload or {}).get("sandbox_id") or "").strip()
    if not sandbox_id:
        raise SandboxInvalidInput("sandbox_id is required")
    state = get(sandbox_id)
    if state is None:
        raise SandboxNotFound(f"sandbox '{sandbox_id}' is not active on this host")
    return state
