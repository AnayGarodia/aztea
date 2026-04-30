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
    ARXIV_RESEARCH_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    HN_DIGEST_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    SHELL_EXECUTOR_AGENT_ID,
    TYPE_CHECKER_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
    WEB_RESEARCHER_AGENT_ID,
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
    # Image Generator: model-backed image generation is priced above local
    # deterministic tools and scales with image count.
    IMAGE_GENERATOR_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "image_count",
            "unit": "image",
            "min_cents": 10,
            "max_cents": 600,
            "tiers": [
                {"up_to_units": 1, "cents": 10},
                {"up_to_units": 3, "cents": 25},
                {"up_to_units": 6, "cents": 45},
            ],
            "multipliers": {"high_res": 1.5},
        },
    },
    # arXiv Research: 3¢ per paper returned, floor 5¢.
    ARXIV_RESEARCH_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "max_results",
            "unit": "paper",
            "rate_cents_per_unit": 3,
            "min_cents": 5,
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
    # Web Researcher: tiered by URL count.
    WEB_RESEARCHER_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "urls",
            "unit": "url",
            "min_cents": 2,
            "tiers": [
                {"up_to_units": 1, "cents": 2},
                {"up_to_units": 3, "cents": 5},
                {"up_to_units": 6, "cents": 9},
                {"up_to_units": 10, "cents": 15},
            ],
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
    # Shell Executor: 1¢ flat per call (fixed, not variable).
    # Stored here so display_min_price_usd() can report the correct floor.
    SHELL_EXECUTOR_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "timeout",
            "unit": "second",
            "rate_cents_per_unit": 0,
            "min_cents": 1,
        },
    },
    # Type Checker: 1¢ flat per call.
    TYPE_CHECKER_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "timeout",
            "unit": "second",
            "rate_cents_per_unit": 0,
            "min_cents": 1,
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
}


def get_pricing_overlay() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the overlay for consumption by part_008."""
    return {k: dict(v) for k, v in _OVERLAY.items()}


def display_min_price_usd(agent_id: str, platform_fee_pct: float = 10.0) -> float | None:
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
