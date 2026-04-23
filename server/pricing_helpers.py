"""Variable-pricing request helpers shared across server shards.

Kept outside the ``application_parts`` folder so the shard files stay
under the 1000-line CI budget. The functions here depend only on
``core.registry`` and ``core.payments`` — no server globals — and are
intentionally side-effect-light so the unit-test harness can import
them without booting the whole FastAPI application.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from core import payments
from core import registry

_LOG = logging.getLogger("aztea.pricing")


def _usd_to_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except Exception:
        return 0
    if not amount.is_finite() or amount < 0:
        return 0
    cents = int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if amount > 0 and cents == 0:
        return 1
    return cents


def _builtin_pricing_overlay_initial() -> dict[str, dict[str, Any]]:
    try:
        from server.builtin_agents import get_pricing_overlay
        return get_pricing_overlay()
    except Exception:  # pragma: no cover - defensive
        return {}


builtin_pricing_overlay: dict[str, dict[str, Any]] = _builtin_pricing_overlay_initial()


def resolve_agent_pricing(agent: dict) -> tuple[str, dict | None]:
    """Return the effective (pricing_model, pricing_config) for an agent."""
    pricing_model = str(agent.get("pricing_model") or "fixed").strip().lower()
    pricing_config = agent.get("pricing_config")
    if pricing_model == "fixed" or not pricing_config:
        overlay = builtin_pricing_overlay.get(agent.get("agent_id"))
        if overlay is not None:
            return overlay["pricing_model"], overlay.get("pricing_config")
    return pricing_model, pricing_config if isinstance(pricing_config, dict) else None


def estimate_variable_charge(
    *,
    agent: dict,
    payload: dict | None,
    budget_cents: int | None = None,
    per_job_cap_cents: int | None = None,
) -> dict[str, Any]:
    """Compute effective per-call price in cents + cap-violation metadata."""
    pricing_model, pricing_config = resolve_agent_pricing(agent)
    fixed_cents = _usd_to_cents(agent.get("price_per_call_usd") or 0)
    estimate = registry.estimate_price_cents(
        pricing_model=pricing_model,
        pricing_config=pricing_config,
        payload=payload,
        fixed_price_cents=fixed_cents,
    )
    price_cents = int(estimate["price_cents"])
    caps: list[tuple[str, int]] = []
    if budget_cents is not None:
        caps.append(("budget", int(budget_cents)))
    if per_job_cap_cents is not None:
        caps.append(("per_job_cap", int(per_job_cap_cents)))
    result: dict[str, Any] = {
        "price_cents": price_cents,
        "pricing_model": estimate["pricing_model"],
        "units": estimate.get("units"),
        "unit": estimate.get("unit"),
        "detail": estimate.get("detail"),
        "fixed_cents": fixed_cents,
        "cap_violated": None,
    }
    for name, cap in caps:
        if price_cents > cap:
            result["cap_violated"] = {
                "scope": name, "limit_cents": cap, "price_cents": price_cents,
            }
            break
    return result


def maybe_refund_pricing_diff(
    *,
    agent: dict,
    payload: dict | None,
    output: dict | None,
    caller_wallet_id: str,
    charge_tx_id: str,
    estimate: dict,
    caller_charge_cents: int,
    success_distribution: dict,
) -> int:
    """Insert a compensating refund when actual usage < estimated usage.

    Agents report their actual quantity under ``billing_units_actual``
    or under the same key as ``pricing_config.input_field``. When it's
    lower than the pre-charge estimate we recompute the charge, and
    refund the difference to the caller. The original charge row is
    never mutated — the ledger stays insert-only.
    """
    if not isinstance(output, dict) or not output:
        return 0
    pricing_model, pricing_config = resolve_agent_pricing(agent)
    if pricing_model == "fixed" or not pricing_config:
        return 0
    input_field = str(pricing_config.get("input_field") or "").strip()
    actual_raw = output.get("billing_units_actual")
    if actual_raw is None and input_field:
        actual_raw = output.get(input_field)
    if actual_raw is None:
        return 0
    try:
        actual_units = max(0, int(actual_raw))
    except (TypeError, ValueError):
        return 0
    original_units = int(estimate.get("units") or 0)
    if actual_units >= original_units:
        return 0
    actual_payload = dict(payload or {})
    if input_field:
        actual_payload[input_field] = actual_units
    actual_estimate = registry.estimate_price_cents(
        pricing_model=pricing_model,
        pricing_config=pricing_config,
        payload=actual_payload,
        fixed_price_cents=int(estimate.get("fixed_cents") or 0),
    )
    actual_price_cents = int(actual_estimate["price_cents"])
    if actual_price_cents >= int(estimate.get("price_cents") or 0):
        return 0
    actual_distribution = payments.compute_success_distribution(
        actual_price_cents,
        platform_fee_pct=int(payments.PLATFORM_FEE_PCT),
        fee_bearer_policy="caller",
    )
    actual_caller_cents = int(actual_distribution["caller_charge_cents"])
    diff_cents = max(0, caller_charge_cents - actual_caller_cents)
    if diff_cents <= 0:
        return 0
    try:
        payments.post_call_refund_difference(
            caller_wallet_id,
            charge_tx_id,
            diff_cents,
            str(agent.get("agent_id") or ""),
            memo=(
                f"Variable-pricing adjustment: billed {original_units} "
                f"{estimate.get('unit') or 'units'}, actual {actual_units}."
            ),
        )
    except Exception:
        _LOG.exception("Variable-pricing refund failed for charge %s", charge_tx_id)
        return 0
    return diff_cents
