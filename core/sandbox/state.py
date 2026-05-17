"""Process-local registry of live sandboxes + on-disk persistence root.

# OWNS: the global ``SandboxState`` map and the ``/tmp/aztea-sandbox`` directory
#       layout used for secrets, snapshots, recordings, and audit logs.
# NOT OWNS: Docker subprocess calls, signing, or any business logic.
# INVARIANTS:
#   * All mutations to ``_REGISTRY`` go through ``register/get/remove`` so the
#     module-level RLock serialises concurrent agent calls.
#   * On-disk paths are derived only from sandbox_id — never from user input —
#     so a malicious payload can't traverse out of the state directory.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.sandbox.models import (
    DEFAULT_AUTO_SNAPSHOT_MIN,
    DEFAULT_IDLE_KILL_MIN,
    DEFAULT_MAX_LIFETIME_MIN,
    SANDBOX_ID_PREFIX,
    SandboxStatus,
    now_unix,
)

# Bare-character sandbox_id allowlist. We accept the project-wide ``sbx_``
# prefix plus hex/underscores, so log lines stay greppable.
_SBX_ID_RE = re.compile(r"^sbx_[a-f0-9]{16,32}$")
_STATE_ROOT_ENV = "AZTEA_SANDBOX_STATE_ROOT"
_DEFAULT_STATE_ROOT = "/tmp/aztea-sandbox"


def state_root() -> Path:
    """Resolve the on-disk state directory; create it if absent."""
    raw = os.environ.get(_STATE_ROOT_ENV) or _DEFAULT_STATE_ROOT
    root = Path(raw).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def sandbox_dir(sandbox_id: str) -> Path:
    """Return the per-sandbox state directory; create it if absent."""
    if not is_valid_sandbox_id(sandbox_id):
        raise ValueError(f"invalid sandbox_id: {sandbox_id!r}")
    d = state_root() / sandbox_id
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def is_valid_sandbox_id(sandbox_id: str) -> bool:
    """Pure: ``True`` if ``sandbox_id`` matches the project allowlist."""
    return isinstance(sandbox_id, str) and bool(_SBX_ID_RE.match(sandbox_id))


def generate_sandbox_id() -> str:
    """Generate an unguessable sandbox_id with the project prefix."""
    return f"{SANDBOX_ID_PREFIX}{secrets.token_hex(12)}"


@dataclass
class BootInfo:
    """Pure-data: strategy + project name + service map captured at boot."""

    strategy: str
    project_name: str
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    boot_log_tail: str = ""
    boot_timing: dict[str, float] = field(default_factory=dict)
    detected_postgres_service: str | None = None
    detected_postgres_db: str | None = None
    detected_postgres_user: str | None = None


@dataclass
class LifetimePolicy:
    """Pure-data: server-side lifetime caps for a single sandbox."""

    max_minutes: int = DEFAULT_MAX_LIFETIME_MIN
    idle_kill_minutes: int = DEFAULT_IDLE_KILL_MIN
    auto_snapshot_every_minutes: int = DEFAULT_AUTO_SNAPSHOT_MIN
    snapshot_on_stop: bool = True


@dataclass
class NetworkPolicyState:
    """Pure-data: resolved per-sandbox network policy."""

    egress: str = "isolated"
    egress_allowlist: list[str] = field(default_factory=list)


@dataclass
class SandboxState:
    """In-memory record for one live sandbox.

    Why: keeping state out of the DB (for now) lets the engine survive
    inside any deployment without a schema migration. Persistence-on-disk
    handles things that must survive a host restart (snapshots, secrets,
    audit log) but the operational registry is process-local — a host
    restart implies sandboxes are stopped anyway.
    """

    sandbox_id: str
    status: SandboxStatus
    created_at: int
    expires_at: int
    last_activity_at: int
    last_snapshot_at: int
    workspace_id: str | None
    owner_hint: str | None
    region: str
    size: dict[str, Any]
    lifetime: LifetimePolicy
    network: NetworkPolicyState
    boot: BootInfo
    filesystem_root: str
    snapshot_chain: list[str] = field(default_factory=list)
    bg_processes: dict[str, dict[str, Any]] = field(default_factory=dict)
    cookie_jar_path: str = ""
    receipts_count: int = 0
    last_audit_hash: str = ""
    failure_reason: str | None = None

    def touch(self) -> None:
        """Side-effect: bump ``last_activity_at`` to ``now``.

        Any non-trivial call should touch the sandbox; the idle sweeper
        uses this to decide on auto-stop.
        """
        self.last_activity_at = now_unix()


# --- Module-level registry ---------------------------------------------------

_REGISTRY: dict[str, SandboxState] = {}
_REGISTRY_LOCK = threading.RLock()


def register(state: SandboxState) -> None:
    """Insert a new sandbox; refuses to overwrite an existing entry."""
    with _REGISTRY_LOCK:
        if state.sandbox_id in _REGISTRY:
            raise RuntimeError(f"sandbox already registered: {state.sandbox_id}")
        _REGISTRY[state.sandbox_id] = state


def get(sandbox_id: str) -> SandboxState | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(sandbox_id)


def remove(sandbox_id: str) -> SandboxState | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(sandbox_id, None)


def list_all() -> list[SandboxState]:
    """Snapshot copy so callers can iterate without holding the lock."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def reset_for_tests() -> None:
    """Side-effect: drop the in-memory registry. Tests only."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def project_name_for(sandbox_id: str) -> str:
    """Pure: derive the Docker compose project name from sandbox_id.

    Compose accepts ``[a-z0-9][a-z0-9_-]*``; the ``sbx_`` prefix already fits.
    """
    return sandbox_id.replace("_", "-")


def epoch_minute_offset(minutes: int) -> int:
    """Pure: ``now + minutes`` as Unix epoch seconds."""
    return int(time.time()) + int(minutes) * 60
