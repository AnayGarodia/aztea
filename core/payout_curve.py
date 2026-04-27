"""Quality-adjusted payout curves for agent listings.

An agent can declare a payout_curve on registration:
    {"1": 0.0, "2": 0.0, "3": 0.5, "4": 1.0, "5": 1.0}

When a caller rates a completed job, if the agent has a payout curve and the
rating maps to a fraction < 1.0, the platform issues a compensating clawback:
debit the agent wallet and credit the caller wallet for the withheld portion.
This encodes "pay for quality" as a marketplace primitive without routing
everything through the dispute system.

Rules:
- Only applies when the job is settled (charge_tx_id present, settled_at set).
- Only applies once per job (idempotency key: job_id + "payout_curve").
- The clawback is on agent_payout_cents, not caller_charge_cents. Platform fee
  is never reversed by the curve — only the agent's share is reduced.
- fraction=1.0 → no clawback (agent keeps everything).
- fraction=0.0 → agent's full payout returned to caller.
"""

from __future__ import annotations

import json
import logging
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from core import db as _db

_LOG = logging.getLogger(__name__)

_VALID_STARS = {"1", "2", "3", "4", "5"}
_DEFAULT_CURVE: dict[str, float] = {}  # empty = no curve, always full payout


def parse_curve(raw: Any) -> dict[str, float] | None:
    """Parse and validate a payout curve from a JSON string or dict.

    Returns a normalised {star: fraction} dict, or None if raw is empty/null.
    Raises ValueError for invalid input.
    """
    if raw is None or raw == "" or raw == "null":
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"payout_curve must be valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("payout_curve must be a JSON object mapping star ratings to fractions.")
    result: dict[str, float] = {}
    for key, val in raw.items():
        star = str(key).strip()
        if star not in _VALID_STARS:
            raise ValueError(f"payout_curve keys must be '1'–'5', got '{star}'.")
        try:
            fraction = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"payout_curve['{star}'] must be a number, got {val!r}.")
        if not (0.0 <= fraction <= 1.0):
            raise ValueError(f"payout_curve['{star}'] must be between 0.0 and 1.0.")
        result[star] = fraction
    return result or None


def fraction_for_rating(curve: dict[str, float] | None, rating: int) -> float:
    """Return the payout fraction [0.0, 1.0] for a given star rating."""
    if not curve:
        return 1.0
    return float(curve.get(str(rating), 1.0))


def curve_to_json(curve: dict[str, float] | None) -> str | None:
    if not curve:
        return None
    return json.dumps({k: float(v) for k, v in curve.items()}, sort_keys=True)


def apply_curve_clawback(
    *,
    job_id: str,
    agent_id: str,
    agent_wallet_id: str,
    caller_wallet_id: str,
    agent_payout_cents: int,
    payout_fraction: float,
) -> dict[str, Any]:
    """Issue a compensating clawback from agent→caller when payout_fraction < 1.

    Returns a summary dict describing what was done. Idempotent: if a clawback
    transaction for this job already exists, returns without double-charging.
    """
    if payout_fraction >= 1.0:
        return {"clawback_cents": 0, "payout_fraction": 1.0, "applied": False}

    clawback_cents = int(
        (Decimal(str(agent_payout_cents)) * (1 - Decimal(str(payout_fraction))))
        .quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if clawback_cents <= 0:
        return {"clawback_cents": 0, "payout_fraction": payout_fraction, "applied": False}

    idempotency_key = f"payout_curve:{job_id}"
    db_path = _db.DB_PATH
    pkg = __import__("sys").modules.get("core.payments")
    if pkg is not None:
        candidate = getattr(pkg, "DB_PATH", None)
        if isinstance(candidate, str) and candidate:
            db_path = candidate

    with _db.get_raw_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM transactions WHERE memo = ? LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {"clawback_cents": clawback_cents, "payout_fraction": payout_fraction, "applied": False, "reason": "already_applied"}

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        clawback_tx_id = str(uuid.uuid4())

        # Debit agent wallet
        conn.execute(
            """
            INSERT INTO transactions (tx_id, wallet_id, type, amount_cents, agent_id, memo, created_at)
            VALUES (?, ?, 'payout_curve_clawback', ?, ?, ?, ?)
            """,
            (clawback_tx_id, agent_wallet_id, -clawback_cents, agent_id, idempotency_key, now),
        )
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ? WHERE wallet_id = ?",
            (clawback_cents, agent_wallet_id),
        )

        # Credit caller wallet
        refund_tx_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO transactions (tx_id, wallet_id, type, amount_cents, agent_id, related_tx_id, memo, created_at)
            VALUES (?, ?, 'payout_curve_refund', ?, ?, ?, ?, ?)
            """,
            (refund_tx_id, caller_wallet_id, clawback_cents, agent_id, clawback_tx_id, idempotency_key, now),
        )
        conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents + ? WHERE wallet_id = ?",
            (clawback_cents, caller_wallet_id),
        )

    _LOG.info(
        "payout_curve.clawback job=%s agent=%s fraction=%.2f clawback_cents=%d",
        job_id, agent_id, payout_fraction, clawback_cents,
    )
    return {"clawback_cents": clawback_cents, "payout_fraction": payout_fraction, "applied": True}
