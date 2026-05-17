"""Canonical variable-pricing overlay for built-in agents.

The per-agent UPDATE in ``server.application_parts.part_001`` does not
populate the ``pricing_model`` / ``pricing_config`` columns for built-in
listings (part_001 is intentionally read-only for this change). At
runtime, ``server.application_parts.part_008`` consults this overlay so
variable pricing applies to built-in agents even on databases that
migrated from the fixed-price schema.

Each entry maps a built-in agent UUID to an object with the same shape
the external-agent write path accepts:

    {
        "pricing_model": "per_unit" | "tiered",
        "pricing_config": { ... }
    }

Adding a new built-in agent that uses variable pricing? Register it
here; no migration required.
"""

from __future__ import annotations

from typing import Any

from server.builtin_agents.constants import (
    CVELOOKUP_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    HN_DIGEST_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
)

_OVERLAY: dict[str, dict[str, Any]] = {
    # Video Storyboard: model-backed video rendering is materially more
    # expensive than local deterministic tools, so keep a higher floor.
    VIDEO_STORYBOARD_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "duration_seconds",
            "unit": "second",
            "rate_cents_per_unit": 4,
            "min_cents": 30,
            "max_cents": 800,
        },
    },
    # Python Executor: deterministic local sandboxing; keep it at the 1¢ floor.
    PYTHON_EXECUTOR_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "timeout",
            "unit": "second",
            "rate_cents_per_unit": 0,
            "min_cents": 1,
        },
    },
    # HN Digest: 2¢ per story returned, floor 5¢.
    HN_DIGEST_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "count",
            "unit": "story",
            "rate_cents_per_unit": 2,
            "min_cents": 5,
        },
    },
    # DNS Inspector: tiered by domain count.
    DNS_INSPECTOR_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "domains",
            "unit": "domain",
            "min_cents": 1,
            "tiers": [
                {"up_to_units": 1, "cents": 1},
                {"up_to_units": 3, "cents": 3},
                {"up_to_units": 10, "cents": 8},
            ],
        },
    },
    # CVE Lookup is a free, platform-subsidized gateway agent. The tiered
    # structure is preserved so the variable-pricing code path stays
    # exercised; rates are zeroed out across every tier. Flip back to non-
    # zero rates if the gateway-tier policy ever changes.
    CVELOOKUP_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "cve_ids",
            "fallback_input_fields": ["packages", "cve_id"],
            "unit": "CVE",
            "min_cents": 0,
            "tiers": [
                {"up_to_units": 1, "cents": 0},
                {"up_to_units": 5, "cents": 0},
                {"up_to_units": 10, "cents": 0},
            ],
        },
    },
}


def get_pricing_overlay() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the overlay for consumption by part_008."""
    return {k: dict(v) for k, v in _OVERLAY.items()}


def display_min_price_usd(
    agent_id: str, platform_fee_pct: float = 10.0
) -> float | None:
    """Return the minimum caller-visible USD price for a variable-priced built-in.

    Returns None for agents not in the overlay (they use a fixed spec price).
    Includes the platform fee because callers pay price + fee.
    """
    entry = _OVERLAY.get(agent_id)
    if entry is None:
        return None
    config = entry.get("pricing_config", {})
    min_cents = config.get("min_cents", 0)
    caller_min_cents = min_cents * (1 + platform_fee_pct / 100)
    return round(caller_min_cents / 100, 4)
