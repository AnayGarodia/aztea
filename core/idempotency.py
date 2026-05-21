"""Server-side idempotency for high-cost POSTs.

# OWNS: dedup of (owner_id, scope, idempotency_key) submissions against
#       the existing ``idempotency_requests`` table from migration 0034.
# NOT OWNS: per-tool client-side dedup. MCP / SDK callers that want
#           local-only dedup can hash request bodies themselves; this
#           module is the server-side hard guarantee.
# INVARIANTS:
#   * Status starts at 'in_progress' on first INSERT and transitions to
#     'completed' once the handler finishes successfully. Failed
#     handlers DELETE the row so the caller can retry cleanly.
#   * Reuse within ``_IDEMPOTENCY_TTL_SECONDS`` (24h) returns the cached
#     response. Past TTL the row is treated as expired and overwritten.
#   * A second submission with the same key but a DIFFERENT request_hash
#     returns 409 ``idempotency.payload_mismatch`` — the contract is a
#     replay safety net, not a fuzzy-match.
#   * A second submission while the first is still ``in_progress`` returns
#     409 ``idempotency.in_progress`` with a retry_after_seconds hint.

C2 follow-up, 2026-05-19: wires up the ``idempotency_requests`` table
that's been on the schema since migration 0034 but had only the Stripe
topup writer using it. Now hire_batch (and any future high-cost POST)
shares the same primitives.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from core import db as _db

_LOG = logging.getLogger("aztea.idempotency")
DB_PATH = _db.DB_PATH
_local = _db._local

# 24h matches the documented contract for hire_batch dedup. Long enough
# to absorb every realistic retry burst (operator restart, intermittent
# network) without indefinitely pinning storage.
_IDEMPOTENCY_TTL_SECONDS = 24 * 3600

# How long a caller should wait before retrying a submission whose
# original is still in_progress. Real handlers finish in seconds; this
# is the upper bound the caller can confidently retry against.
_IN_PROGRESS_RETRY_AFTER_SECONDS = 30


def _conn() -> _db.DbConnection:
    return _db.get_db_connection(_resolved_db_path())


def _resolved_db_path() -> str:
    """Prefer ``core.idempotency.DB_PATH`` for isolated tests."""
    module = sys.modules.get("core.idempotency")
    if module is not None:
        candidate = getattr(module, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return DB_PATH


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_request_hash(body: dict[str, Any] | None) -> str:
    """Pure: canonical SHA-256 of the request body for replay matching.

    Strips the idempotency_key field itself before hashing — otherwise a
    caller re-sending the SAME idempotency_key in a different envelope
    would mismatch its own replay.
    """
    payload = dict(body or {})
    payload.pop("idempotency_key", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class IdempotencyResult:
    """Discriminated wrapper for the four possible outcomes of a check."""

    __slots__ = ("kind", "cached_response", "retry_after_seconds", "stored_hash")

    def __init__(
        self,
        *,
        kind: str,
        cached_response: dict[str, Any] | None = None,
        retry_after_seconds: int | None = None,
        stored_hash: str | None = None,
    ) -> None:
        self.kind = kind
        self.cached_response = cached_response
        self.retry_after_seconds = retry_after_seconds
        self.stored_hash = stored_hash


def begin(
    *,
    owner_id: str,
    scope: str,
    idempotency_key: str,
    request_hash: str,
) -> IdempotencyResult:
    """Side-effect: claim or look up an idempotency row.

    Returns one of:
      - kind='proceed' — first time for this key; row inserted, caller
        runs the handler and must call ``complete`` on success or
        ``release`` on failure.
      - kind='cached' — completed reuse within TTL; cached_response holds
        the prior response body verbatim.
      - kind='in_progress' — duplicate submission while the first is
        still running; retry_after_seconds is set.
      - kind='payload_mismatch' — same key, different request_hash;
        stored_hash carries the original for the error envelope.
    """
    if not idempotency_key:
        raise ValueError("idempotency_key must be a non-empty string")
    now = _now()
    cutoff = (now - timedelta(seconds=_IDEMPOTENCY_TTL_SECONDS)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT status, request_hash, response_body, response_status,
                   created_at, updated_at
            FROM idempotency_requests
            WHERE owner_id = %s AND scope = %s AND idempotency_key = %s
            """,
            (owner_id, scope, idempotency_key),
        ).fetchone()
        if row is not None:
            created_at = str(row["created_at"] or "")
            if created_at and created_at < cutoff:
                # Expired — drop the stale row and fall through to fresh INSERT.
                conn.execute(
                    """
                    DELETE FROM idempotency_requests
                    WHERE owner_id = %s AND scope = %s AND idempotency_key = %s
                    """,
                    (owner_id, scope, idempotency_key),
                )
            else:
                stored_hash = str(row["request_hash"] or "")
                if stored_hash and stored_hash != request_hash:
                    return IdempotencyResult(
                        kind="payload_mismatch", stored_hash=stored_hash,
                    )
                if str(row["status"]) == "completed":
                    body_raw = row["response_body"]
                    try:
                        cached = json.loads(body_raw) if body_raw else None
                    except (TypeError, json.JSONDecodeError):
                        cached = None
                    return IdempotencyResult(
                        kind="cached",
                        cached_response=cached if isinstance(cached, dict) else None,
                    )
                # status == 'in_progress'
                return IdempotencyResult(
                    kind="in_progress",
                    retry_after_seconds=_IN_PROGRESS_RETRY_AFTER_SECONDS,
                )
        try:
            conn.execute(
                """
                INSERT INTO idempotency_requests
                    (owner_id, scope, idempotency_key, request_hash, status,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, 'in_progress', %s, %s)
                """,
                (
                    owner_id,
                    scope,
                    idempotency_key,
                    request_hash,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        except _db.IntegrityError:
            # Race: a concurrent request inserted between our SELECT and
            # our INSERT. Re-read; the concurrent inserter owns this key.
            row = conn.execute(
                """
                SELECT status, request_hash FROM idempotency_requests
                WHERE owner_id = %s AND scope = %s AND idempotency_key = %s
                """,
                (owner_id, scope, idempotency_key),
            ).fetchone()
            if row is None:
                # Genuinely odd — the other inserter rolled back. Try once more.
                return IdempotencyResult(kind="proceed")
            if str(row["request_hash"] or "") != request_hash:
                return IdempotencyResult(
                    kind="payload_mismatch",
                    stored_hash=str(row["request_hash"] or ""),
                )
            return IdempotencyResult(
                kind="in_progress",
                retry_after_seconds=_IN_PROGRESS_RETRY_AFTER_SECONDS,
            )
    return IdempotencyResult(kind="proceed")


def complete(
    *,
    owner_id: str,
    scope: str,
    idempotency_key: str,
    response_status: int,
    response_body: dict[str, Any],
) -> None:
    """Side-effect: mark the in_progress row as completed and store the response."""
    if not idempotency_key:
        return
    now_iso = _now().isoformat()
    body_json = json.dumps(response_body, default=str)
    with _conn() as conn:
        conn.execute(
            """
            UPDATE idempotency_requests
            SET status = 'completed',
                response_status = %s,
                response_body = %s,
                updated_at = %s
            WHERE owner_id = %s AND scope = %s AND idempotency_key = %s
            """,
            (response_status, body_json, now_iso, owner_id, scope, idempotency_key),
        )


def release(*, owner_id: str, scope: str, idempotency_key: str) -> None:
    """Side-effect: drop the in_progress row so a retry starts fresh.

    Called when the handler raises before reaching ``complete`` — without
    this the row would block subsequent retries with in_progress until the
    TTL ticks down.
    """
    if not idempotency_key:
        return
    try:
        with _conn() as conn:
            conn.execute(
                """
                DELETE FROM idempotency_requests
                WHERE owner_id = %s AND scope = %s AND idempotency_key = %s
                  AND status = 'in_progress'
                """,
                (owner_id, scope, idempotency_key),
            )
    except Exception:  # noqa: BLE001 — release must never block the failure
        _LOG.warning(
            "idempotency.release failed for owner=%s scope=%s key=%r",
            owner_id, scope, idempotency_key, exc_info=True,
        )
