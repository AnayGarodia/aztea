"""
ingest.py — validate + append-only insert of Otto telemetry events.

The Otto app POSTs a batch of events (see docs/otto-telemetry-schema.md). We
validate the envelope, extract a small set of denormalized "hot" columns from
`task` events so the dashboard never parses JSON, and insert append-only with
event_id as the dedup key. A replayed/duplicated event_id is silently ignored
(ON CONFLICT DO NOTHING / INSERT OR IGNORE), so the app can retry sends and
flush an offline queue without ever double-counting.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core import db as _db

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Max events accepted in one POST. The app batches; a larger payload is almost
# certainly abuse or a bug, so we reject loudly rather than silently truncate.
MAX_BATCH = 100

ALLOWED_EVENTS = frozenset(
    {"install", "launch", "task", "permission", "onboarding", "account", "error", "download"}
)

# Allowed enum values we defensively clamp on write, so a malformed client can't
# pollute the slice dimensions the dashboard groups by. Unknown → "other"/None.
_INTENTS = frozenset(
    {"form_fill", "email", "research", "file_op", "navigation", "data_entry", "scheduling", "other"}
)
_OUTCOMES = frozenset({"success", "partial", "failed", "stopped"})
_FAILURE_REASONS = frozenset(
    {
        "none", "element_not_found", "stale_ref", "verify_failed", "vision_timeout",
        "repeat_loop", "user_stopped", "model_refused", "app_error", "network",
    }
)
_SUMMON = frozenset({"voice", "typed"})

# Columns on otto_telemetry_events, in insert order. Kept here so the INSERT and
# the row builder can't drift apart.
_COLUMNS = (
    "event_id", "event", "schema_version", "device_id", "session_id",
    "app_version", "os_version", "mac_model", "ts_client", "ts_server", "day",
    "props",
    "intent_category", "app", "outcome", "failure_reason", "summon", "from_recipe",
    "total_ms", "ttfa_ms", "model_ms", "perceive_ms", "act_ms", "verify_ms",
    "vision_steps", "step_count", "cost_usd",
)


@dataclass
class IngestResult:
    """Outcome of an ingest call. `accepted` excludes duplicates and rejects."""

    received: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors[:10],  # cap so a bad batch can't return a wall of text
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _clamp(value: Any, allowed: frozenset[str], default: str | None) -> str | None:
    """Return value if it's an allowed enum member, else default. Defends the
    dashboard's group-by dimensions from malformed clients."""
    if isinstance(value, str) and value in allowed:
        return value
    return default


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _hot_columns(event: str, props: dict[str, Any]) -> dict[str, Any]:
    """Extract denormalized task columns. Non-task events leave them NULL."""
    cols: dict[str, Any] = {
        "intent_category": None, "app": None, "outcome": None, "failure_reason": None,
        "summon": None, "from_recipe": None, "total_ms": None, "ttfa_ms": None,
        "model_ms": None, "perceive_ms": None, "act_ms": None, "verify_ms": None,
        "vision_steps": None, "step_count": None, "cost_usd": None,
    }
    if event != "task":
        return cols
    latency = props.get("latency_ms") or {}
    path = props.get("path") or {}
    cols.update(
        {
            "intent_category": _clamp(props.get("intent_category"), _INTENTS, "other"),
            "app": (str(props.get("app"))[:120] if props.get("app") is not None else None),
            "outcome": _clamp(props.get("outcome"), _OUTCOMES, None),
            "failure_reason": _clamp(props.get("failure_reason"), _FAILURE_REASONS, "none"),
            "summon": _clamp(props.get("summon"), _SUMMON, None),
            "from_recipe": 1 if props.get("from_recipe") else 0,
            "total_ms": _as_int(latency.get("total")),
            "ttfa_ms": _as_int(latency.get("ttfa")),
            "model_ms": _as_int(latency.get("model")),
            "perceive_ms": _as_int(latency.get("perceive")),
            "act_ms": _as_int(latency.get("act")),
            "verify_ms": _as_int(latency.get("verify")),
            "vision_steps": _as_int(path.get("vision")),
            "step_count": _as_int(props.get("step_count")),
            "cost_usd": _as_float(props.get("cost_usd")),
        }
    )
    return cols


def _build_row(event: dict[str, Any], ts_server: str, day: str) -> tuple | None:
    """Validate one event and return its column tuple, or None if invalid."""
    if not isinstance(event, dict):
        return None
    name = event.get("event")
    if name not in ALLOWED_EVENTS:
        return None
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        return None
    props = event.get("props")
    if not isinstance(props, dict):
        props = {}

    hot = _hot_columns(name, props)
    record = {
        "event_id": event_id.strip()[:64],
        "event": name,
        "schema_version": _as_int(event.get("schema_version")) or SCHEMA_VERSION,
        "device_id": (str(event.get("device_id"))[:64] if event.get("device_id") else None),
        "session_id": (str(event.get("session_id"))[:64] if event.get("session_id") else None),
        "app_version": (str(event.get("app_version"))[:40] if event.get("app_version") else None),
        "os_version": (str(event.get("os_version"))[:80] if event.get("os_version") else None),
        "mac_model": (str(event.get("mac_model"))[:80] if event.get("mac_model") else None),
        "ts_client": (str(event.get("ts_client"))[:40] if event.get("ts_client") else None),
        "ts_server": ts_server,
        "day": day,
        "props": json.dumps(props, default=str)[:8000],
        **hot,
    }
    return tuple(record[col] for col in _COLUMNS)


def _insert_sql() -> str:
    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    cols = ", ".join(_COLUMNS)
    base = f"INSERT INTO otto_telemetry_events ({cols}) VALUES ({placeholders})"
    # Dedup on event_id. db.py doesn't translate upsert syntax, so branch by backend.
    if _db.IS_POSTGRES:
        return base + " ON CONFLICT (event_id) DO NOTHING"
    return base.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)


def ingest_events(events: list[Any]) -> IngestResult:
    """Validate and append-insert a batch. Duplicates (same event_id) are ignored.

    Returns counts so the route can answer the client with accepted/duplicate/
    rejected totals — useful for the app's send-queue bookkeeping.
    """
    result = IngestResult(received=len(events))
    if not isinstance(events, list):
        result.rejected = 1
        result.errors.append("body.events must be a list")
        return result
    if len(events) > MAX_BATCH:
        result.rejected = len(events)
        result.errors.append(f"batch too large: {len(events)} > {MAX_BATCH}")
        return result

    ts_server = _now_iso()
    day = _today()
    sql = _insert_sql()

    with _db.get_db_connection() as conn:
        with conn:  # one transaction for the batch
            for raw in events:
                row = _build_row(raw, ts_server, day)
                if row is None:
                    result.rejected += 1
                    continue
                try:
                    cur = conn.execute(sql, row)
                    # rowcount is 1 on insert, 0 when the ON CONFLICT/IGNORE skipped a dup.
                    if cur.rowcount and cur.rowcount > 0:
                        result.accepted += 1
                    else:
                        result.duplicates += 1
                except _db.IntegrityError:
                    # Belt-and-suspenders: a racing duplicate that slipped past the
                    # upsert clause is a dup, not an error.
                    result.duplicates += 1
                except (_db.OperationalError, _db.ProgrammingError) as exc:
                    logger.exception("otto telemetry insert failed")
                    result.rejected += 1
                    result.errors.append(str(exc)[:200])
    return result


def record_download(
    *, platform: str, referrer: str | None, utm_source: str | None, utm_campaign: str | None
) -> None:
    """Record one website download click as a `download` event. Server-generated
    event_id (the website has no app-side id to dedup on). Best-effort: a logging
    failure must never block the redirect to the DMG."""
    event = {
        "event_id": str(uuid.uuid4()),
        "event": "download",
        "schema_version": SCHEMA_VERSION,
        "props": {
            "platform": (platform or "mac")[:40],
            "referrer": (referrer or "")[:300] or None,
            "utm_source": (utm_source or "")[:120] or None,
            "utm_campaign": (utm_campaign or "")[:120] or None,
        },
    }
    try:
        ingest_events([event])
    except Exception:
        logger.exception("otto telemetry: record_download failed (non-fatal)")
