"""Async job lifecycle: creation, leases, messaging, settlement hooks.

This package replaces the legacy ``core/jobs.py`` module. It is split into
smaller files that each own a coherent slice of the lifecycle:

- ``db`` — SQLite schema, connection helpers, row/JSON utilities, and pure
  persistence helpers (``create_job``, ``get_job``, ``update_job_status`` …).
- ``crud`` — higher-level read/update helpers that compose the ``db`` primitives
  (listings, authorisation checks, filters).
- ``leases`` — claim, release, heartbeat, expired-lease scanning, correlation-id
  bookkeeping for tool calls and streamed messages.
- ``messaging`` — typed job messages (progress, clarification, tool calls,
  quality/dispute outcome writes, SSE-friendly row projection).

The package ``__init__`` merges each submodule's public surface into a single
namespace so callers can keep writing ``from core import jobs`` and then
``jobs.create_job(...)``, ``jobs.claim_job(...)``, ``jobs.add_message(...)`` etc.
Later submodules override earlier ones on name collisions — this is intentional
so that the ``messaging`` implementations of ``set_job_quality_result`` /
``set_job_dispute_outcome`` (which correctly import ``get_job`` from ``.crud``)
win over the older stubs in ``db``.
"""
from __future__ import annotations

from . import crud
from . import db
from . import leases
from . import messaging

for _mod in (db, crud, leases, messaging):
    for _n in dir(_mod):
        if _n.startswith("__"):
            continue
        globals()[_n] = getattr(_mod, _n)
