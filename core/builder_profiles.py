"""
builder_profiles.py — pure aggregation for `/builders/<username>` profile pages.

# OWNS: read-side aggregation of every public-facing number on a builder's
#       profile (agent count, total calls served, average rating, total
#       earnings if opted-in, trust score).
# NOT OWNS: write side (lives in core.auth.users / Stripe settler), the
#       HTTP route (lives in server/application_parts/part_007.py), the
#       opt-in flag itself (lives in users.profile_visible_earnings,
#       added by migration 0073).
# INVARIANTS:
#   - Earnings are NEVER included unless `users.profile_visible_earnings = 1`.
#     The publisher must opt in. Default is 0 ⇒ field omitted entirely
#     (not zeroed) so the frontend can hide the section, not show "$0".
#   - Trust score is the mean of caller_ratings, NOT a per-agent average
#     averaged again — the latter biases low-volume agents.
#   - Unknown username ⇒ raises BuilderNotFound (separate from "builder
#     exists but has no agents", which returns a populated zero-state).

The single entrypoint `build_profile(username, requesting_owner_id=None)` is
called by the route. Tests mock the DB connection so the aggregator can be
exercised without a live database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core import db as _db

_LOG = logging.getLogger(__name__)

__all__ = ["BuilderProfile", "BuilderNotFound", "build_profile"]


class BuilderNotFound(LookupError):
    """Raised when no `users` row matches the requested username."""


@dataclass(frozen=True)
class BuilderProfile:
    """The public-facing builder profile.

    `total_earnings_usd` is OMITTED (not zeroed) when the builder hasn't
    opted in — the route layer drops it from the response so the frontend
    can hide the section entirely. `agents` carries enough per-agent fields
    for the profile page to render a list without a second round-trip.
    """
    username: str
    user_id: str
    agent_count: int
    total_calls_served: int
    average_rating: float | None
    trust_score: float | None
    earnings_visible: bool
    total_earnings_usd: float | None
    agents: list[dict[str, Any]] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "username": self.username,
            "user_id": self.user_id,
            "agent_count": self.agent_count,
            "total_calls_served": self.total_calls_served,
            "average_rating": self.average_rating,
            "trust_score": self.trust_score,
            "earnings_visible": self.earnings_visible,
            "agents": list(self.agents),
        }
        # Only include earnings when opted in. None / missing reads cleanly
        # on the frontend as "hide the section" vs "show $0".
        if self.earnings_visible and self.total_earnings_usd is not None:
            out["total_earnings_usd"] = round(float(self.total_earnings_usd), 2)
        return out


def build_profile(username: str) -> BuilderProfile:
    """Aggregate a builder's public-facing profile by username.

    Reads `users`, `agents`, `transactions` (when earnings are opted in),
    and `caller_ratings` (when present). All reads happen inside a single
    connection from `core.db.get_db_connection`, but as separate SELECTs
    — none of the queries individually justify a join.

    Raises BuilderNotFound when the username is unknown.
    """
    username_norm = (username or "").strip()
    if not username_norm:
        raise BuilderNotFound("Empty username")

    with _db.get_db_connection() as conn:
        user_row = conn.execute(
            "SELECT user_id, username, profile_visible_earnings "
            "FROM users WHERE username = %s",
            (username_norm,),
        ).fetchone()
        if user_row is None:
            raise BuilderNotFound(f"No builder named {username_norm!r}")
        user_id = _row_value(user_row, 0, "user_id")
        earnings_visible = bool(_row_value(user_row, 2, "profile_visible_earnings") or 0)

        agents_rows = conn.execute(
            "SELECT agent_id, slug, name, description, "
            "price_per_call_usd, category, total_calls, success_rate "
            "FROM agents WHERE owner_id = %s "
            "  AND (review_status IS NULL OR review_status NOT IN ('sunset', 'banned')) "
            "ORDER BY total_calls DESC NULLS LAST, created_at DESC",
            (user_id,),
        ).fetchall()

        agents_summary = [_agent_row_to_dict(row) for row in agents_rows]
        agent_count = len(agents_summary)
        total_calls_served = sum(int(a.get("total_calls") or 0) for a in agents_summary)

        average_rating = _fetch_average_rating(conn, user_id)
        trust_score = _fetch_trust_score(conn, user_id)
        total_earnings_usd = (
            _fetch_total_earnings(conn, user_id) if earnings_visible else None
        )

    return BuilderProfile(
        username=username_norm,
        user_id=str(user_id),
        agent_count=agent_count,
        total_calls_served=total_calls_served,
        average_rating=average_rating,
        trust_score=trust_score,
        earnings_visible=earnings_visible,
        total_earnings_usd=total_earnings_usd,
        agents=agents_summary,
    )


# ─── Helpers ───────────────────────────────────────────────────────────────


def _row_value(row: Any, index: int, key: str) -> Any:
    """Read a column out of either a tuple-shaped row or a dict-shaped row.

    SQLite returns sqlite3.Row (dict-like with column-name access); psycopg2
    can return either. We normalize at the call site so the rest of the
    aggregator stays oblivious."""
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        try:
            return row[index]
        except (TypeError, IndexError):
            return None


def _agent_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "agent_id": _row_value(row, 0, "agent_id"),
        "slug": _row_value(row, 1, "slug"),
        "name": _row_value(row, 2, "name"),
        "description": _row_value(row, 3, "description"),
        "price_per_call_usd": _row_value(row, 4, "price_per_call_usd"),
        "category": _row_value(row, 5, "category"),
        "total_calls": _row_value(row, 6, "total_calls") or 0,
        "success_rate": _row_value(row, 7, "success_rate"),
    }


def _fetch_average_rating(conn: Any, user_id: str) -> float | None:
    """Mean caller rating across every agent this user owns.

    NULL when there are zero ratings — distinguishes "unrated" from "1.0".
    """
    # Narrowed from bare `except Exception` (/review 2026-05-27): we only
    # want to swallow the "table doesn't exist in this fixture" case —
    # OperationalError covers SQLite, ProgrammingError covers Postgres for
    # the same "missing relation" condition. Genuine bugs (constraint
    # violations, type errors) propagate, and the structured log makes
    # silent degradation visible during ops triage.
    _expected_db_errs = (_db.OperationalError, _db.ProgrammingError)
    try:
        row = conn.execute(
            "SELECT AVG(cr.rating) "
            "FROM caller_ratings cr "
            "JOIN agents a ON a.agent_id = cr.agent_id "
            "WHERE a.owner_id = %s",
            (user_id,),
        ).fetchone()
    except _expected_db_errs as exc:
        _LOG.warning(
            "builder_profile.average_rating_unavailable user_id=%s reason=%s",
            user_id, exc,
        )
        return None
    if row is None:
        return None
    val = _row_value(row, 0, "AVG(cr.rating)")
    return float(val) if val is not None else None


def _fetch_trust_score(conn: Any, user_id: str) -> float | None:
    """Weighted trust = mean(agents.trust_score) per owner.

    Per-agent trust scores are populated by core.reputation; we average them
    here for the profile rollup. NULL when no agents have trust signals yet.
    """
    _expected_db_errs = (_db.OperationalError, _db.ProgrammingError)
    try:
        row = conn.execute(
            "SELECT AVG(trust_score) FROM agents "
            "WHERE owner_id = %s AND trust_score IS NOT NULL",
            (user_id,),
        ).fetchone()
    except _expected_db_errs as exc:
        _LOG.warning(
            "builder_profile.trust_score_unavailable user_id=%s reason=%s",
            user_id, exc,
        )
        return None
    if row is None:
        return None
    val = _row_value(row, 0, "AVG(trust_score)")
    return float(val) if val is not None else None


def _fetch_total_earnings(conn: Any, user_id: str) -> float | None:
    """Lifetime USD earnings, summed across every payout entry on every
    agent this user owns. Only called when earnings_visible=True.

    Pulled from `transactions` where `type='payout'` (the agent-side
    settlement entry). Sum is in cents; convert to USD at the boundary.
    """
    _expected_db_errs = (_db.OperationalError, _db.ProgrammingError)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(t.amount_cents), 0) "
            "FROM transactions t "
            "JOIN wallets w ON w.wallet_id = t.wallet_id "
            "WHERE w.owner_id = %s AND t.type = 'payout'",
            (user_id,),
        ).fetchone()
    except _expected_db_errs as exc:
        _LOG.warning(
            "builder_profile.earnings_unavailable user_id=%s reason=%s",
            user_id, exc,
        )
        return None
    cents = _row_value(row, 0, "COALESCE(SUM(t.amount_cents), 0)") if row else None
    if cents is None:
        return 0.0
    return float(cents) / 100.0
