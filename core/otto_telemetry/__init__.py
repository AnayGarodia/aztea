"""
core/otto_telemetry — anonymous product telemetry from the Otto macOS app.

OWNS: validation + append-only ingest of Otto telemetry events, and the
      read-only aggregation queries that power the /admin/otto dashboard.

NOT OWNS: the HTTP routes (server/routes/otto_telemetry.py), the schema/DDL
          (migrations/0086_otto_telemetry.sql), money/ledger (telemetry never
          touches the ledger — cost_usd here is an app-side estimate echoed for
          product analytics, not settlement).

INVARIANTS:
  - Append-only. One row per event; event_id is the dedup key (retries/offline
    queue replays never double-count).
  - Privacy: no raw task text, no file names, no argument values are stored.
    Only category labels + structured numbers. device_id is anonymous.
  - Every aggregation is read-only (SELECT only).
  - SQL stays portable across SQLite (dev/tests) and Postgres (prod): CASE/WHEN
    not FILTER, app-side percentiles, 0/1 integers for booleans, grouping on the
    pre-computed `day` column rather than backend date functions.

See docs/otto-telemetry-schema.md for the event contract (mirrored in the Otto
repo). Bump schema_version in both repos together when the contract changes.
"""

from __future__ import annotations

from core.otto_telemetry.ingest import (
    ALLOWED_EVENTS,
    SCHEMA_VERSION,
    IngestResult,
    ingest_events,
    record_download,
)
from core.otto_telemetry.metrics import SECTIONS, compute_section

__all__ = [
    "ALLOWED_EVENTS",
    "SCHEMA_VERSION",
    "IngestResult",
    "ingest_events",
    "record_download",
    "SECTIONS",
    "compute_section",
]
