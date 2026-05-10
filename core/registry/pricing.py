"""Variable-pricing helpers for agent listings.

An agent row may carry a ``pricing_model`` column in one of three values:

- ``fixed`` (default) — caller is charged ``price_per_call_usd`` for every
  invocation, regardless of input shape.
- ``per_unit`` — a linear rate is applied to an integer quantity that is
  extracted from the input payload. Config keys:

    {
        "unit": "second",                 # human-readable unit label
        "rate_cents_per_unit": 50,        # cents charged per unit
        "min_cents": 100,                 # optional floor
        "max_cents": 2500,                # optional ceiling
        "input_field": "duration_seconds",
        "multipliers": {"high_res": 2}    # optional input-flag multipliers
    }

- ``tiered`` — the cents amount of the first tier whose ``up_to_units``
  threshold covers the requested quantity. Config keys:

    {
        "unit": "image",
        "input_field": "image_count",
        "min_cents": 100,
        "max_cents": 5000,
        "tiers": [
            {"up_to_units": 1,  "cents": 150},
            {"up_to_units": 4,  "cents": 500},
            {"up_to_units": 10, "cents": 1200}
        ]
    }

The *callers* of this module decide where the pricing config comes from.
External listings persist it on the ``agents`` row; built-in listings
declare it in ``server.builtin_agents`` and fall through to the overlay
helper ``builtin_pricing_overlay`` for agent IDs that pre-date the
database column.

Every price returned by ``estimate_price_cents`` is an integer number of
cents and is guaranteed to be finite, non-negative, and clamped to the
configured ``max_cents`` (if any). Downstream callers must still enforce
``budget_cents`` and ``per_job_cap_cents``; this module intentionally
does not know about those.
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from core.functional import Err, Ok, Result

VALID_PRICING_MODELS = ("fixed", "per_unit", "tiered")
_CENTS_PER_USD = Decimal("100")


class VariablePricingError(ValueError):
    """Raised for malformed pricing configs or bad inputs."""


def normalize_pricing_model(value: Any) -> str:
    """Normalise a pricing model string to lowercase. Defaults to 'fixed' for blank input. Raises VariablePricingError for unknown models."""
    text = str(value or "").strip().lower()
    if not text:
        return "fixed"
    if text not in VALID_PRICING_MODELS:
        raise VariablePricingError(
            f"pricing_model must be one of {VALID_PRICING_MODELS}, got {value!r}."
        )
    return text


def _to_int(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise VariablePricingError(f"{field} must be an integer, got a boolean.")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise VariablePricingError(f"{field} must be an integer, got {value!r}.")
    if parsed < minimum:
        raise VariablePricingError(f"{field} must be >= {minimum}, got {parsed}.")
    return parsed


def _to_positive_decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise VariablePricingError(f"{field} must be numeric, got {value!r}.") from exc
    if not parsed.is_finite() or parsed < 0:
        raise VariablePricingError(f"{field} must be a non-negative number.")
    return parsed


def _parse_pricing_config_dict(model: str, pricing_config: Any) -> dict[str, Any]:
    """Pure: decode pricing_config (dict or JSON string) into a dict; raises VariablePricingError otherwise."""
    if pricing_config is None or pricing_config == "":
        raise VariablePricingError(
            f"pricing_config is required when pricing_model is {model!r}."
        )
    if isinstance(pricing_config, str):
        try:
            parsed_raw: Any = json.loads(pricing_config)
        except json.JSONDecodeError as exc:
            raise VariablePricingError(
                f"pricing_config is not valid JSON: {exc}"
            ) from exc
    else:
        parsed_raw = pricing_config
    if not isinstance(parsed_raw, dict):
        raise VariablePricingError("pricing_config must be an object.")
    return parsed_raw


def _parse_pricing_bounds(
    parsed_raw: dict[str, Any],
) -> tuple[str, str, int, int | None]:
    """Pure: extract ``(input_field, unit, min_cents, max_cents)``; raises on missing field."""
    input_field = str(parsed_raw.get("input_field") or "").strip()
    if not input_field:
        raise VariablePricingError("pricing_config.input_field is required.")
    unit = str(parsed_raw.get("unit") or input_field).strip() or input_field
    min_cents = _to_int(
        parsed_raw.get("min_cents", 0), field="pricing_config.min_cents"
    )
    max_raw = parsed_raw.get("max_cents")
    max_cents = (
        _to_int(max_raw, field="pricing_config.max_cents", minimum=0)
        if max_raw is not None else None
    )
    if max_cents is not None and max_cents < min_cents:
        raise VariablePricingError("pricing_config.max_cents must be >= min_cents.")
    return input_field, unit, min_cents, max_cents


def _parse_pricing_multipliers(parsed_raw: dict[str, Any]) -> dict[str, float] | None:
    """Pure: validate ``multipliers`` and convert each entry to a positive float."""
    multipliers_raw = parsed_raw.get("multipliers")
    if multipliers_raw is None:
        return None
    if not isinstance(multipliers_raw, dict):
        raise VariablePricingError("pricing_config.multipliers must be an object.")
    canonical_mults: dict[str, float] = {}
    for key, value in multipliers_raw.items():
        try:
            factor = float(value)
        except (TypeError, ValueError):
            raise VariablePricingError(
                f"pricing_config.multipliers[{key!r}] must be numeric."
            )
        if not math.isfinite(factor) or factor <= 0:
            raise VariablePricingError(
                f"pricing_config.multipliers[{key!r}] must be a positive number."
            )
        canonical_mults[str(key)] = factor
    return canonical_mults or None


def _parse_pricing_tiers(parsed_raw: dict[str, Any]) -> list[dict[str, int]]:
    """Pure: validate the ``tiers`` array; ascending by ``up_to_units``; raises otherwise."""
    tiers_raw = parsed_raw.get("tiers")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise VariablePricingError(
            "pricing_config.tiers must be a non-empty list for tiered pricing."
        )
    tiers: list[dict[str, int]] = []
    last_threshold: int | None = None
    for idx, tier in enumerate(tiers_raw):
        if not isinstance(tier, dict):
            raise VariablePricingError(
                f"pricing_config.tiers[{idx}] must be an object."
            )
        up_to = _to_int(
            tier.get("up_to_units"),
            field=f"pricing_config.tiers[{idx}].up_to_units",
            minimum=1,
        )
        cents = _to_int(
            tier.get("cents"),
            field=f"pricing_config.tiers[{idx}].cents",
            minimum=0,
        )
        if last_threshold is not None and up_to <= last_threshold:
            raise VariablePricingError(
                "pricing_config.tiers must be sorted ascending by up_to_units."
            )
        last_threshold = up_to
        tiers.append({"up_to_units": up_to, "cents": cents})
    return tiers


def validate_pricing_config(
    pricing_model: str, pricing_config: Any
) -> dict[str, Any] | None:
    """Pure: canonicalise a pricing_config dict, or return ``None`` for ``fixed``.

    Why: surfaces every shape error as a typed ``VariablePricingError`` so
    the registration boundary returns one actionable message per failure.
    """
    model = normalize_pricing_model(pricing_model)
    if model == "fixed":
        return None
    parsed_raw = _parse_pricing_config_dict(model, pricing_config)
    input_field, unit, min_cents, max_cents = _parse_pricing_bounds(parsed_raw)
    canonical: dict[str, Any] = {
        "input_field": input_field,
        "unit": unit,
        "min_cents": min_cents,
        "max_cents": max_cents,
    }
    multipliers = _parse_pricing_multipliers(parsed_raw)
    if multipliers:
        canonical["multipliers"] = multipliers
    if model == "per_unit":
        rate = _to_positive_decimal(
            parsed_raw.get("rate_cents_per_unit"),
            field="pricing_config.rate_cents_per_unit",
        )
        canonical["rate_cents_per_unit"] = float(rate)
    else:
        canonical["tiers"] = _parse_pricing_tiers(parsed_raw)
    canonical["pricing_model"] = model
    return canonical


def validate_pricing_config_result(
    pricing_model: str, pricing_config: Any
) -> "Result[dict[str, Any] | None, str]":
    """Result-returning variant of :func:`validate_pricing_config`.

    Returns ``Ok(canonical_config)`` or ``Err(message)``.  Use this in new
    code so validation errors are explicit rather than caught exceptions.
    """
    try:
        return Ok(validate_pricing_config(pricing_model, pricing_config))
    except VariablePricingError as exc:
        return Err(str(exc))


def parse_pricing_config(raw: Any) -> dict[str, Any]:
    """Best-effort parse of a stored JSON pricing_config. Never raises."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _resolve_quantity_field_names(config: Mapping[str, Any]) -> list[str]:
    """Pure: ordered ``input_field`` + fallback list with empty entries dropped."""
    names = [str(config.get("input_field") or "").strip()]
    fallback_fields = config.get("fallback_input_fields")
    if isinstance(fallback_fields, list):
        names.extend(str(item or "").strip() for item in fallback_fields)
    return [field for field in names if field]


def _lookup_quantity_value(
    payload: Mapping[str, Any], field_names: list[str],
) -> tuple[Any, str]:
    """Pure: walk payload + nested ``input`` block; returns ``(raw_value, field_used)``."""
    for field in field_names:
        if field in payload:
            return payload.get(field), field
    inner = payload.get("input")
    if isinstance(inner, Mapping):
        for field in field_names:
            if field in inner:
                return inner.get(field), field
    # WHY: a URL probe with no explicit count implies one request.
    if "url" in payload and "request_count" in field_names:
        return 1, ""
    return None, ""


def _coerce_quantity(raw: Any) -> int:
    """Pure: coerce a quantity value (int / float / str / list) to a non-negative int; 0 otherwise."""
    if isinstance(raw, bool) or raw is None:
        return 0
    if isinstance(raw, (int, float)):
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0
    if isinstance(raw, list):
        return len(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return 0
        try:
            return max(0, int(float(stripped)))
        except (TypeError, ValueError):
            return 0
    return 0


def _quantity_from_payload(
    payload: Mapping[str, Any] | None,
    config: Mapping[str, Any],
) -> int:
    """Pure: derive billable quantity from ``payload`` per the pricing config."""
    if not isinstance(payload, Mapping):
        return 0
    field_names = _resolve_quantity_field_names(config)
    if not field_names:
        return 0
    raw, _ = _lookup_quantity_value(payload, field_names)
    return _coerce_quantity(raw)


def _apply_multipliers(
    base_cents: int,
    payload: Mapping[str, Any] | None,
    multipliers: Mapping[str, float] | None,
) -> int:
    if not multipliers or not isinstance(payload, Mapping):
        return base_cents
    total = Decimal(base_cents)
    for key, factor in multipliers.items():
        if bool(payload.get(key)):
            total = total * Decimal(str(factor))
    cents = int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return max(0, cents)


def _clamp(value: int, *, min_cents: int, max_cents: int | None) -> int:
    if value < min_cents:
        value = min_cents
    if max_cents is not None and value > max_cents:
        value = max_cents
    return max(0, int(value))


def _fixed_price_response(fixed_price_cents: int) -> dict[str, Any]:
    """Pure: response shape for the fixed-pricing path."""
    return {
        "price_cents": max(0, int(fixed_price_cents)),
        "pricing_model": "fixed",
        "units": 1,
        "unit": None,
        "detail": "per-call price",
    }


def _estimate_per_unit(
    config: dict[str, Any], payload: Mapping[str, Any] | None,
    units: int, unit: str, min_cents: int, max_cents: int | None,
    multipliers: dict[str, float] | None,
) -> dict[str, Any]:
    """Pure: shape per-unit pricing into the standard response dict.

    Why: when min_cents floors a tiny computed amount we surface that
    explicitly so callers never see confusing strings like "0 seconds @ 0¢/second".
    """
    rate = float(config.get("rate_cents_per_unit") or 0.0)
    base = int(
        Decimal(rate * max(0, units)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    with_mults = _apply_multipliers(base, payload, multipliers)
    price_cents = _clamp(with_mults, min_cents=min_cents, max_cents=max_cents)
    natural_detail = f"{units} {unit}{'s' if units != 1 else ''} @ {rate:g}¢/{unit}"
    detail = (
        f"{natural_detail} (floor {min_cents}¢ applied)"
        if min_cents and price_cents == min_cents and base < min_cents
        else natural_detail
    )
    return {
        "price_cents": price_cents,
        "pricing_model": "per_unit",
        "units": units,
        "unit": unit,
        "detail": detail,
    }


def _select_tier(
    tiers: list, units: int, fixed_price_cents: int,
) -> tuple[int, int | None]:
    """Pure: pick the matching tier for ``units``. ``(cents, threshold_or_None)``.

    Why: when ``units`` exceeds the top tier we cap at the last tier's price
    rather than reverting to the fixed-fee path, which would be a stealth
    overcharge for high-volume calls.
    """
    chosen_cents: int | None = None
    chosen_threshold: int | None = None
    for tier in tiers:
        if not isinstance(tier, Mapping):
            continue
        threshold = int(tier.get("up_to_units") or 0)
        if units <= threshold:
            return int(tier.get("cents") or 0), threshold
    if tiers:
        last = tiers[-1]
        if isinstance(last, Mapping):
            chosen_cents = int(last.get("cents") or 0)
            chosen_threshold = int(last.get("up_to_units") or 0)
    if chosen_cents is None:
        return int(fixed_price_cents), None
    return chosen_cents, chosen_threshold


def _estimate_tiered(
    config: dict[str, Any], payload: Mapping[str, Any] | None,
    units: int, unit: str, min_cents: int, max_cents: int | None,
    multipliers: dict[str, float] | None, fixed_price_cents: int,
) -> dict[str, Any]:
    """Pure: shape tiered pricing into the standard response dict."""
    chosen_cents, chosen_threshold = _select_tier(
        config.get("tiers") or [], units, fixed_price_cents,
    )
    with_mults = _apply_multipliers(chosen_cents, payload, multipliers)
    price_cents = _clamp(with_mults, min_cents=min_cents, max_cents=max_cents)
    threshold_label = chosen_threshold if chosen_threshold is not None else "∞"
    return {
        "price_cents": price_cents,
        "pricing_model": "tiered",
        "units": units,
        "unit": unit,
        "detail": (
            f"{units} {unit}{'s' if units != 1 else ''} · tier up to {threshold_label} {unit}"
        ),
    }


def estimate_price_cents(
    *,
    pricing_model: str,
    pricing_config: Any,
    payload: Mapping[str, Any] | None,
    fixed_price_cents: int,
) -> dict[str, Any]:
    """Pure: compute the caller charge (in cents) for one invocation.

    Why: bad configs fall back to the fixed price so in-flight calls are
    never blocked by a pricing regression. Returns ``{price_cents,
    pricing_model, units, unit, detail}``.
    """
    model = str(pricing_model or "").strip().lower() or "fixed"
    config = parse_pricing_config(pricing_config) if model != "fixed" else {}
    if model == "fixed" or not config:
        return _fixed_price_response(fixed_price_cents)
    units = _quantity_from_payload(payload, config)
    min_cents = int(config.get("min_cents") or 0)
    max_cents_raw = config.get("max_cents")
    max_cents = int(max_cents_raw) if max_cents_raw is not None else None
    unit = str(config.get("unit") or config.get("input_field") or "unit")
    multipliers = (
        config.get("multipliers")
        if isinstance(config.get("multipliers"), dict) else None
    )
    if model == "per_unit":
        return _estimate_per_unit(
            config, payload, units, unit, min_cents, max_cents, multipliers,
        )
    if model == "tiered":
        return _estimate_tiered(
            config, payload, units, unit, min_cents, max_cents, multipliers, fixed_price_cents,
        )
    return _fixed_price_response(fixed_price_cents)


def price_usd_to_cents(value: Any) -> int:
    """Convert a USD price (float, str, or Decimal) to integer cents, rounded half-up.

    Returns 0 for invalid, negative, or non-finite inputs rather than raising.
    """
    try:
        amount = Decimal(str(value))
    except Exception:
        return 0
    if not amount.is_finite() or amount < 0:
        return 0
    cents = int(
        (amount * _CENTS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if amount > 0 and cents == 0:
        return 1
    return cents
