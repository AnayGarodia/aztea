"""Cross-process registry of live sandboxes + on-disk persistence root.

# OWNS: the ``SandboxState`` map (in-memory cache + per-sandbox JSON files on
#       disk) and the ``/tmp/aztea-sandbox`` directory layout used for
#       secrets, snapshots, recordings, and audit logs.
# NOT OWNS: Docker subprocess calls, signing, or any business logic.
# INVARIANTS:
#   * All mutations to ``_REGISTRY`` go through ``register/get/remove/save``
#     so the module-level RLock serialises concurrent agent calls.
#   * On-disk paths are derived only from sandbox_id — never from user input —
#     so a malicious payload can't traverse out of the state directory.
#   * The on-disk JSON files under ``{state_root}/_registry/`` are the SSOT
#     across uvicorn workers — see ``_save``/``_load_from_disk`` below.
# DECISIONS:
#   * v0 keeps the in-memory dict as a write-through cache; touch() flushes to
#     disk so the sweeper running on any worker sees fresh activity. The disk
#     write is small (<10 KB per sandbox) and happens at the cadence of
#     human-perceivable agent operations, so write amplification is fine.
"""

from __future__ import annotations

import dataclasses
import json
import logging
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

_LOG = logging.getLogger("aztea.sandbox.state")
_REGISTRY_DIR_NAME = "_registry"

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
        """Side-effect: bump ``last_activity_at`` to ``now`` and flush to disk.

        Any non-trivial call should touch the sandbox; the idle sweeper
        uses this to decide on auto-stop. The disk flush is required for
        cross-worker visibility — a sweeper running on uvicorn worker B
        must see touches performed on worker A so it doesn't idle-kill a
        sandbox that's actively serving exec calls.
        """
        self.last_activity_at = now_unix()
        _save(self)

    @property
    def ttl_remaining_seconds(self) -> int:
        """B18, 2026-05-19: visibility on wall-clock TTL burn-down.

        Pure: ``max(0, expires_at - now())``. Returned in every sandbox
        response (sandbox_start, sandbox_status, sandbox_exec envelope)
        so callers can see exactly how much budget remains BEFORE firing
        an expensive op (snapshot / fork / docker commit). The TTL model
        is wall-clock; expensive operations DO consume the quota — this
        property lets you observe the consumption rather than learn
        about it post-mortem.
        """
        return max(0, int(self.expires_at - now_unix()))


# --- Module-level registry ---------------------------------------------------
#
# uvicorn runs Aztea with ``--workers N`` (currently 2). Each worker is its
# own Python process with its own ``_REGISTRY`` dict — so a sandbox created
# on worker A was invisible to worker B pre-fix, and ``sandbox_exec`` would
# return ``not_found`` half the time when round-robined across workers (the
# 2026-05-18 test report's exec/status disagreement bug). The fix: persist
# each ``SandboxState`` to ``{state_root}/_registry/{sandbox_id}.json`` so
# the disk is the cross-worker SSOT. The in-memory dict stays as a read
# cache to avoid a disk hit on every operation within the worker that
# created the sandbox.

_REGISTRY: dict[str, SandboxState] = {}
_REGISTRY_LOCK = threading.RLock()


def _registry_dir() -> Path:
    """Resolve (and create) the directory that holds per-sandbox state files."""
    d = state_root() / _REGISTRY_DIR_NAME
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _state_file(sandbox_id: str) -> Path:
    """Pure: the on-disk JSON path for a sandbox's serialized state."""
    if not is_valid_sandbox_id(sandbox_id):
        raise ValueError(f"invalid sandbox_id: {sandbox_id!r}")
    return _registry_dir() / f"{sandbox_id}.json"


def _serialize(state: SandboxState) -> dict[str, Any]:
    """Pure: dataclass → JSON-safe dict for on-disk persistence."""
    return dataclasses.asdict(state)


def _deserialize(raw: dict[str, Any]) -> SandboxState:
    """Pure: rehydrate a SandboxState from disk, reconstructing nested dataclasses.

    Why: ``dataclasses.asdict`` flattens nested dataclasses to plain dicts;
    on read we have to rebuild ``BootInfo`` / ``LifetimePolicy`` /
    ``NetworkPolicyState`` instances explicitly or the engine's
    attribute-style access (``state.boot.strategy``) breaks.
    """
    payload = dict(raw)
    boot = BootInfo(**payload.pop("boot"))
    lifetime = LifetimePolicy(**payload.pop("lifetime"))
    network = NetworkPolicyState(**payload.pop("network"))
    return SandboxState(boot=boot, lifetime=lifetime, network=network, **payload)


def _save(state: SandboxState) -> None:
    """Side-effect: atomically write the sandbox state to its registry file.

    Atomic via tmp-file + ``replace`` so a partial write never leaves a
    half-baked JSON on disk for another worker to read. Errors are logged
    but never raised — a transient disk hiccup shouldn't blow up an exec
    call that already succeeded in-memory.
    """
    try:
        path = _state_file(state.sandbox_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(_serialize(state), separators=(",", ":")))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError as exc:
        _LOG.warning(
            "sandbox.state.persist_failed",
            extra={"sandbox_id": state.sandbox_id, "error": str(exc)},
        )


def _load_from_disk(sandbox_id: str) -> SandboxState | None:
    """Side-effect: read + parse one sandbox's state file; ``None`` if absent or corrupt."""
    try:
        path = _state_file(sandbox_id)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        return _deserialize(raw)
    except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
        _LOG.warning(
            "sandbox.state.load_failed",
            extra={"sandbox_id": sandbox_id, "error": str(exc)},
        )
        return None


def register(state: SandboxState) -> None:
    """Insert a new sandbox in memory + on disk; refuses to overwrite."""
    with _REGISTRY_LOCK:
        if state.sandbox_id in _REGISTRY:
            raise RuntimeError(f"sandbox already registered: {state.sandbox_id}")
        # Also refuse if another worker already wrote a file for this ID — at
        # 12 hex bytes the collision probability is astronomical, but the
        # cross-worker race is real and silent corruption is worse than a
        # loud RuntimeError on the (impossible) collision path.
        try:
            disk_path = _state_file(state.sandbox_id)
        except ValueError:
            disk_path = None
        if disk_path is not None and disk_path.exists():
            raise RuntimeError(f"sandbox already registered on disk: {state.sandbox_id}")
        _save(state)
        _REGISTRY[state.sandbox_id] = state


def get(sandbox_id: str) -> SandboxState | None:
    """Return the sandbox if known to this worker OR persisted by any worker."""
    with _REGISTRY_LOCK:
        cached = _REGISTRY.get(sandbox_id)
        if cached is not None:
            return cached
        loaded = _load_from_disk(sandbox_id)
        if loaded is not None:
            _REGISTRY[sandbox_id] = loaded
        return loaded


def remove(sandbox_id: str) -> SandboxState | None:
    """Drop the sandbox from both the in-memory cache and the on-disk SSOT."""
    with _REGISTRY_LOCK:
        try:
            _state_file(sandbox_id).unlink()
        except (FileNotFoundError, ValueError):
            pass
        except OSError as exc:
            _LOG.warning(
                "sandbox.state.remove_failed",
                extra={"sandbox_id": sandbox_id, "error": str(exc)},
            )
        return _REGISTRY.pop(sandbox_id, None)


def list_all() -> list[SandboxState]:
    """Snapshot copy of every sandbox known across all workers.

    Reads the disk SSOT so a worker without prior in-memory knowledge can
    still surface sandboxes registered by sibling workers in
    ``sandbox_list``. Hydrates the in-memory cache for any new entries.
    """
    with _REGISTRY_LOCK:
        merged: dict[str, SandboxState] = dict(_REGISTRY)
        registry_dir = _registry_dir()
        for state_file in registry_dir.glob("*.json"):
            sandbox_id = state_file.stem
            if sandbox_id in merged:
                continue
            loaded = _load_from_disk(sandbox_id)
            if loaded is not None:
                merged[sandbox_id] = loaded
                _REGISTRY[sandbox_id] = loaded
        return list(merged.values())


def reset_for_tests() -> None:
    """Side-effect: drop the in-memory registry AND its on-disk files. Tests only."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
        registry_dir = state_root() / _REGISTRY_DIR_NAME
        if registry_dir.exists():
            for state_file in registry_dir.glob("*.json"):
                try:
                    state_file.unlink()
                except OSError:
                    pass


def project_name_for(sandbox_id: str) -> str:
    """Pure: derive the Docker compose project name from sandbox_id.

    Compose accepts ``[a-z0-9][a-z0-9_-]*``; the ``sbx_`` prefix already fits.
    """
    return sandbox_id.replace("_", "-")


def epoch_minute_offset(minutes: int) -> int:
    """Pure: ``now + minutes`` as Unix epoch seconds."""
    return int(time.time()) + int(minutes) * 60
