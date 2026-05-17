"""Shared types, constants, and exceptions for the live_sandbox engine.

# OWNS: enumerations, constants, exception hierarchy, response shape helpers.
# NOT OWNS: any sandbox state, any Docker calls, any signing.
# INVARIANTS:
#   * Every sandbox action returns a dict with a top-level ``receipt`` (set by
#     the caller after the action runs — receipts are minted in receipts.py).
#   * Time fields are always second-precision Unix epoch ints.
#   * Error envelopes follow the project-wide ``{"error": {"code","message"}}``.
# DECISIONS:
#   * Bash field names (cmd, cwd, stdin, env, exit_code, timed_out, duration_ms)
#     are non-negotiable per spec — must match Claude Code's local Bash shape.
"""

from __future__ import annotations

import time
from typing import Any, Literal

# --- Constants ---------------------------------------------------------------

DEFAULT_MAX_LIFETIME_MIN = 30
HARD_MAX_LIFETIME_MIN = 120
DEFAULT_IDLE_KILL_MIN = 30
DEFAULT_AUTO_SNAPSHOT_MIN = 10
DEFAULT_EXEC_TIMEOUT_S = 300
HARD_EXEC_TIMEOUT_S = 1800
DEFAULT_READY_TIMEOUT_S = 600
DEFAULT_CPU_LIMIT = "2"
DEFAULT_MEMORY_GB = 4
DEFAULT_DISK_GB = 20
DEFAULT_PIDS_LIMIT = 2048
SANDBOX_ID_PREFIX = "sbx_"
SNAPSHOT_ID_PREFIX = "snap_"
FORK_ID_PREFIX = "sbx_"

# Bash-shape exec output fields. Spec is firm: no renaming, ever.
EXEC_RESPONSE_KEYS = (
    "stdout",
    "stderr",
    "exit_code",
    "timed_out",
    "duration_ms",
)

# Action verbs — kept in one place so dispatch + audit + stub registry agree.
ALL_ACTIONS = (
    # lifecycle
    "sandbox_start",
    "sandbox_status",
    "sandbox_stop",
    "sandbox_extend",
    "sandbox_list",
    "sandbox_resume",
    # exec
    "sandbox_exec",
    "sandbox_exec_in_service",
    "sandbox_bg_start",
    "sandbox_bg_list",
    "sandbox_bg_kill",
    "sandbox_bg_logs",
    # filesystem
    "sandbox_read_file",
    "sandbox_write_file",
    "sandbox_delete_file",
    "sandbox_apply_patch",
    "sandbox_glob",
    "sandbox_grep",
    "sandbox_sync_from_local",
    # database
    "sandbox_db_query",
    "sandbox_db_snapshot",
    "sandbox_db_restore",
    "sandbox_db_introspect",
    "sandbox_db_seed",
    # http + logs
    "sandbox_http_request",
    "sandbox_logs",
    "sandbox_metrics",
    "sandbox_inspect_process",
    # snapshots
    "sandbox_snapshot",
    "sandbox_restore",
    "sandbox_fork",
    "sandbox_diff_snapshots",
    # audit + cost
    "sandbox_audit",
    "sandbox_cost",
    "sandbox_quota",
    # stubbed surface
    "sandbox_browser_session",
    "sandbox_browser_navigate",
    "sandbox_browser_click",
    "sandbox_browser_fill",
    "sandbox_browser_screenshot",
    "sandbox_browser_console_logs",
    "sandbox_browser_network",
    "sandbox_browser_a11y_tree",
    "sandbox_browser_eval",
    "sandbox_browser_axe_audit",
    "sandbox_browser_lighthouse",
    "sandbox_browser_record",
    "sandbox_browser_replay",
    "sandbox_tunnel_open",
    "sandbox_tunnel_close",
    "sandbox_webhook_inbox",
    "sandbox_outbound_record",
    "sandbox_outbound_replay",
    "sandbox_inject_failure",
    "sandbox_network_capture",
    "sandbox_trace",
    "sandbox_link",
    "sandbox_batch_start",
    "sandbox_share",
    "sandbox_export_snapshot",
)

NetworkPolicy = Literal["isolated", "allowlist", "open"]
BootStrategy = Literal[
    "auto",
    "docker_compose",
    "dockerfile",
    "devcontainer",
    "custom_commands",
]
SandboxStatus = Literal[
    "booting",
    "ready",
    "running",
    "stopped",
    "failed",
    "suspended",
]


# --- Exceptions --------------------------------------------------------------

class SandboxError(Exception):
    """Base for all engine errors. Carries a stable ``code`` for envelopes."""

    code: str = "sandbox.error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SandboxNotFound(SandboxError):
    code = "sandbox.not_found"


class SandboxBootFailed(SandboxError):
    code = "sandbox.boot_failed"


class SandboxInvalidInput(SandboxError):
    code = "sandbox.invalid_input"


class SandboxServiceMissing(SandboxError):
    code = "sandbox.service_missing"


class SandboxDockerUnavailable(SandboxError):
    code = "sandbox.docker_unavailable"


class SandboxNetworkPolicyDenied(SandboxError):
    code = "sandbox.network_policy_denied"


class SandboxLifetimeExpired(SandboxError):
    code = "sandbox.lifetime_expired"


class SandboxQuotaExceeded(SandboxError):
    code = "sandbox.quota_exceeded"


# --- Envelopes ---------------------------------------------------------------

def error_envelope(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project-canonical error envelope: ``{"error":{"code","message"[,"details"]}}``."""
    if not isinstance(code, str) or not code:
        raise ValueError("error_envelope: code must be a non-empty str")
    if not isinstance(message, str):
        raise TypeError("error_envelope: message must be str")
    err: dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return {"error": err}


def now_unix() -> int:
    """Pure: current Unix epoch seconds. Centralised so tests can monkeypatch."""
    return int(time.time())
