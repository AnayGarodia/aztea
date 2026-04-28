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
import sqlite3
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

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


def _wallet_balance_conn(conn: sqlite3.Connection, wallet_id: str) -> int | None:
    row = conn.execute(
        "SELECT balance_cents FROM wallets WHERE wallet_id = ?",
        (wallet_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["balance_cents"])


def _debit_wallet_conn(
    conn: sqlite3.Connection,
    wallet_id: str,
    amount_cents: int,
    *,
    agent_id: str,
    related_tx_id: str,
    memo: str,
) -> None:
    """Debit a wallet using the same guarded pattern as the main payments flow.

    The payout-curve adjustment is a compensating ledger entry. We therefore use
    the standard `charge`/`refund` transaction types and only insert the ledger
    row after the cached wallet balance update succeeds.
    """
    from core.payments import base as _payments_base

    updated = conn.execute(
        """
        UPDATE wallets
        SET balance_cents = balance_cents - ?
        WHERE wallet_id = ? AND balance_cents >= ?
        """,
        (amount_cents, wallet_id, amount_cents),
    ).rowcount
    if updated == 0:
        balance_cents = _wallet_balance_conn(conn, wallet_id)
        if balance_cents is None:
            raise LookupError(f"Wallet '{wallet_id}' not found.")
        raise _payments_base.InsufficientBalanceError(balance_cents, amount_cents)
    _payments_base._insert_tx_only(
        conn,
        wallet_id,
        "charge",
        -amount_cents,
        agent_id,
        related_tx_id,
        memo,
    )


def _credit_wallet_conn(
    conn: sqlite3.Connection,
    wallet_id: str,
    amount_cents: int,
    *,
    agent_id: str,
    related_tx_id: str,
    memo: str,
) -> None:
    from core.payments import base as _payments_base

    updated = conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents + ? WHERE wallet_id = ?",
        (amount_cents, wallet_id),
    ).rowcount
    if updated == 0:
        raise LookupError(f"Wallet '{wallet_id}' not found.")
    _payments_base._insert_tx_only(
        conn,
        wallet_id,
        "refund",
        amount_cents,
        agent_id,
        related_tx_id,
        memo,
    )


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
    from core.payments import base as _payments_base

    with _payments_base._conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM transactions WHERE memo = ? LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if existing is not None:
            return {"clawback_cents": clawback_cents, "payout_fraction": payout_fraction, "applied": False, "reason": "already_applied"}
        try:
            _debit_wallet_conn(
                conn,
                agent_wallet_id,
                clawback_cents,
                agent_id=agent_id,
                related_tx_id=job_id,
                memo=idempotency_key,
            )
            _credit_wallet_conn(
                conn,
                caller_wallet_id,
                clawback_cents,
                agent_id=agent_id,
                related_tx_id=job_id,
                memo=idempotency_key,
            )
        except (_payments_base.InsufficientBalanceError, LookupError) as exc:
            conn.rollback()
            reason = "wallet_missing" if isinstance(exc, LookupError) else "insufficient_balance"
            _LOG.warning(
                "payout_curve.clawback_skipped job=%s agent=%s reason=%s error=%s",
                job_id,
                agent_id,
                reason,
                exc,
            )
            return {
                "clawback_cents": clawback_cents,
                "payout_fraction": payout_fraction,
                "applied": False,
                "reason": reason,
                "error": str(exc),
            }

    _LOG.info(
        "payout_curve.clawback job=%s agent=%s fraction=%.2f clawback_cents=%d",
        job_id, agent_id, payout_fraction, clawback_cents,
    )
    return {"clawback_cents": clawback_cents, "payout_fraction": payout_fraction, "applied": True}
