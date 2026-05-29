"""hosted_execution_log — fire-and-forget audit of every hosted invocation.

# OWNS: writing one row per /api/playground/test or hosted_skill_call to
#       the ``hosted_execution_log`` table (migration 0072).
# NOT OWNS: the execution itself (``core/skill_executor.py``,
#       ``agents/python_executor.py``), the sandbox runtime
#       (``core/sandbox/``), or downstream analytics (lives in
#       ``server/routes/admin_usage.py`` + dashboards that read this table).
# INVARIANTS:
#   * ``record_execution`` MUST NEVER raise. The call site is the
#     completion path of a billed invocation — a missing log row is a
#     P3 observability gap; a raised exception there is a P0 outage.
#   * No raw input / output is persisted. We store SHA-256 hashes only,
#     so an investigator can correlate repeated identical probes without
#     retaining caller PII or competitor IP from buyer code.
#   * ``execution_id`` is a fresh UUIDv4 every call. Callers may not
#     supply their own to avoid collision shenanigans across surfaces.
# DECISIONS:
#   - Synchronous one-INSERT-one-commit. No background queue, no retry.
#     Pattern matches ``core/registry/decision_audit.py`` (0048).
#   - ``surface`` is a string enum, not an int — JOIN-able with the
#     surface column in any future per-source aggregate.
#   - Hashes only when caller supplies the raw bytes; ``record_execution``
#     hashes here so call sites cannot leak the raw value by accident.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from core import db as _db

_LOG = logging.getLogger(__name__)

Surface = Literal["playground_test", "hosted_skill_call"]
KillReason = Literal["timeout", "oom", "signal", "sandbox_block"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_bytes(payload: Any) -> str | None:
    """Pure: SHA-256-hex of any caller-supplied payload, or None.

    Strings hash directly. Dicts / lists are dumped to canonical JSON
    first so semantically identical payloads produce the same hash.
    Anything else falls back to ``repr()``. NULL passes through.
    """
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    elif isinstance(payload, str):
        data = payload.encode("utf-8", "replace")
    else:
        try:
            data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            data = repr(payload).encode("utf-8", "replace")
    return hashlib.sha256(data).hexdigest()


def record_execution(
    *,
    surface: Surface,
    execution_time_ms: int,
    sandbox_exit_code: int,
    input_payload: Any = None,
    output_payload: Any = None,
    caller_owner_id: str | None = None,
    caller_key_id: str | None = None,
    skill_id: str | None = None,
    peak_memory_mb: float | None = None,
    cpu_seconds: float | None = None,
    was_killed: bool = False,
    kill_reason: KillReason | None = None,
    extra: dict[str, Any] | None = None,
) -> str | None:
    """Append one row to ``hosted_execution_log``. Returns ``execution_id``
    on success, ``None`` on failure. Never raises.

    Why hash here, not at call site: keeps the contract of "no raw
    buyer code in this table" enforceable in one place. Call sites pass
    the raw value; we hash + drop the raw before it touches the DB.

    Why fire-and-forget: this is observability, not business state. A
    write failure must not block the response the buyer sees.
    """
    execution_id = uuid.uuid4().hex
    try:
        input_hash = _hash_bytes(input_payload)
        output_hash = _hash_bytes(output_payload)
        extra_json = (
            json.dumps(extra, sort_keys=True) if isinstance(extra, dict) and extra else None
        )
        with _db.get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO hosted_execution_log (
                    execution_id, surface, caller_owner_id, caller_key_id,
                    skill_id, input_hash, output_hash,
                    execution_time_ms, peak_memory_mb, cpu_seconds,
                    sandbox_exit_code, was_killed, kill_reason,
                    extra_json, created_at
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    execution_id,
                    surface,
                    caller_owner_id,
                    caller_key_id,
                    skill_id,
                    input_hash,
                    output_hash,
                    int(execution_time_ms),
                    float(peak_memory_mb) if peak_memory_mb is not None else None,
                    float(cpu_seconds) if cpu_seconds is not None else None,
                    int(sandbox_exit_code),
                    1 if was_killed else 0,
                    kill_reason,
                    extra_json,
                    _now_iso(),
                ),
            )
            conn.commit()
        return execution_id
    except Exception as exc:  # noqa: BLE001 — observability never raises
        # WARNING (not DEBUG) so prod dashboards surface write failures.
        # The contract is "never raises" — preserved. Visibility into how
        # often the audit table is missing rows matters because abuse
        # investigations and kill-rate dashboards both depend on the
        # table being complete.
        _LOG.warning(
            "hosted_execution_log: write failed for surface=%s skill=%s: %s",
            surface, skill_id, exc,
        )
        return None
