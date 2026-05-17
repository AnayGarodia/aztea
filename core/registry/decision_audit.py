"""
decision_audit.py — fire-and-forget persistence of auto-hire decisions.

OWNS: writes one row to ``auto_hire_decisions`` per ``do_specialist_task`` /
      ``registry_auto_hire`` request, capturing the intent, gating outcome,
      chosen agent, and (when applicable) the resulting job_id.
NOT OWNS: the decision logic itself (``core/registry/auto_hire.py``) and the
      route-level response shape (``server/application_parts/part_012.py``).
INVARIANTS:
  - ``record_decision`` never raises. A write failure is logged at debug level
    and dropped — the request must not be blocked on observability.
  - The function is synchronous but bounded: one INSERT and an immediate
    commit. No retries, no queueing.
DECISIONS:
  - Intent text is truncated to keep rows lean; ``intent_hash`` is a SHA-256
    of the full original text so identical intents cluster cleanly.
  - The candidates JSON is capped at the ``Decision.candidates`` top-N already
    set by ``auto_hire.decide`` (currently 3). Anything richer goes to logs.
KNOWN DEBT:
  - No async write path. If the decision write becomes a bottleneck under
    real traffic, move to a background queue, but only then.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from core import db as _db

logger = logging.getLogger(__name__)

# Why named: the column is TEXT NOT NULL so a missing/empty intent would crash
# the insert. We cap and substitute defensively at the boundary.
_INTENT_TEXT_TRUNCATE = 4096


def _hash_intent(intent_text: str) -> str:
    """Stable SHA-256 of the full intent so identical phrasing groups cleanly."""
    return hashlib.sha256(intent_text.encode("utf-8", errors="replace")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_decision(
    *,
    intent_text: str,
    auto_invoked: bool,
    dry_run: bool = False,
    reason: str | None = None,
    chosen_agent_id: str | None = None,
    confidence: float | None = None,
    candidates: list[dict[str, Any]] | None = None,
    caller_owner_id: str | None = None,
    caller_key_id: str | None = None,
    resulting_job_id: str | None = None,
) -> str | None:
    """Persist one auto-hire decision. Returns the decision_id, or None on failure.

    Why: the gated reasons (no_match, insufficient_confidence, price_exceeded,
    insufficient_trust, ...) are only visible in the HTTP response otherwise,
    which makes "top no-match intents" and "fraction gated vs auto-invoked"
    impossible to answer.
    """
    decision_id = uuid.uuid4().hex
    safe_intent = (intent_text or "")[:_INTENT_TEXT_TRUNCATE]
    intent_hash = _hash_intent(intent_text or "")
    candidates_json = json.dumps(candidates or [], default=str)

    try:
        conn: _db.DbConnection = _db.get_raw_connection(_db.DB_PATH)
        conn.execute(
            """
            INSERT INTO auto_hire_decisions (
                decision_id, caller_owner_id, caller_key_id,
                intent_text, intent_hash, auto_invoked, dry_run, reason,
                chosen_agent_id, confidence, candidates_json,
                resulting_job_id, created_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                decision_id,
                caller_owner_id,
                caller_key_id,
                safe_intent,
                intent_hash,
                1 if auto_invoked else 0,
                1 if dry_run else 0,
                reason,
                chosen_agent_id,
                confidence,
                candidates_json,
                resulting_job_id,
                _now_iso(),
            ),
        )
        conn.commit()
        return decision_id
    except _db.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            logger.debug("decision_audit: write failed: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 — never block the request
        logger.debug("decision_audit: write failed: %s", exc)
        return None
