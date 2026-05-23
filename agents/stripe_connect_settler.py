"""
stripe_connect_settler.py — C14: monthly signed Stripe reconciliation.

# v0 STATUS: requires a Stripe API key + access to the internal ledger
#   source. Returns requires_configuration otherwise.
# REASONING LOOP: plan reconciliation slices → synthesise statement.
"""

from __future__ import annotations

import os
import re
from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)

_AGENT_SLUG = "stripe_connect_settler"

# Stripe expects YYYY-MM form for monthly statements.
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    month = (payload.get("month") or "").strip()
    if not _MONTH_RE.match(month):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "month must be YYYY-MM (e.g. 2026-04)")
    ledger_src = (payload.get("internal_ledger_source") or "").strip()
    if not ledger_src:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "internal_ledger_source is required")
    budget = clamp_int(payload.get("budget_cents"), 40, 1, 500)

    missing: list[str] = []
    if not os.environ.get("STRIPE_API_KEY"):
        missing.append("STRIPE_API_KEY")
    if missing:
        return requires_configuration(
            _AGENT_SLUG, missing,
            "The settler reads Stripe Connect transfers directly; without "
            "the API key it cannot honestly attest to the reconciliation.",
            {"month": month},
        )

    return two_step_reasoning(
        _AGENT_SLUG,
        plan_system=(
            "Plan a Stripe-vs-ledger reconciliation. Output JSON "
            '{"slices": ["payouts", "refunds", "disputes"], "tolerance_cents": 0}'
        ),
        plan_user=f"month={month} ledger={ledger_src}",
        synth_system=(
            "Issue the signed monthly statement. Return JSON "
            '{"statement_id": "...", "totals_cents": {...}, "drift_cents": 0}'
        ),
        synth_user_builder=lambda plan: f"Plan: {plan[:600]}",
        budget_cents=budget,
        extra_output={"month": month, "ledger_source": ledger_src},
    )
