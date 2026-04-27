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
    GITHUB_FETCHER_AGENT_ID,
    HN_DIGEST_AGENT_ID,
    IMAGE_GENERATOR_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    SHELL_EXECUTOR_AGENT_ID,
    TYPE_CHECKER_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
    WEB_RESEARCHER_AGENT_ID,
)


_OVERLAY: dict[str, dict[str, Any]] = {
    # Video Storyboard: $0.03/s of finished video, floor $0.20,
    # ceiling $5.00 so no caller accidentally spends more than $5 on
    # one storyboard.
    VIDEO_STORYBOARD_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "duration_seconds",
            "unit": "second",
            "rate_cents_per_unit": 3,
            "min_cents": 20,
            "max_cents": 500,
        },
    },
    # Image Generator: tiered by image_count, with a 2x multiplier when
    # ``high_res`` is truthy.
    IMAGE_GENERATOR_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "image_count",
            "unit": "image",
            "min_cents": 5,
            "max_cents": 600,
            "tiers": [
                {"up_to_units": 1, "cents": 5},
                {"up_to_units": 3, "cents": 12},
                {"up_to_units": 6, "cents": 24},
            ],
            "multipliers": {"high_res": 2.0},
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
    # Python Executor: 2¢ per second of timeout requested, floor 5¢.
    PYTHON_EXECUTOR_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "timeout",
            "unit": "second",
            "rate_cents_per_unit": 2,
            "min_cents": 5,
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
    # GitHub Fetcher: tiered by file count.
    GITHUB_FETCHER_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "paths",
            "unit": "file",
            "min_cents": 3,
            "tiers": [
                {"up_to_units": 1, "cents": 3},
                {"up_to_units": 5, "cents": 8},
                {"up_to_units": 20, "cents": 18},
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
    # Shell Executor: 3¢ flat per call (fixed, not variable).
    # Stored here so display_min_price_usd() can report the correct floor.
    SHELL_EXECUTOR_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "timeout",
            "unit": "second",
            "rate_cents_per_unit": 0,
            "min_cents": 3,
        },
    },
    # Type Checker: 2¢ flat per call.
    TYPE_CHECKER_AGENT_ID: {
        "pricing_model": "per_unit",
        "pricing_config": {
            "input_field": "timeout",
            "unit": "second",
            "rate_cents_per_unit": 0,
            "min_cents": 2,
        },
    },
    # DNS Inspector: tiered by domain count.
    DNS_INSPECTOR_AGENT_ID: {
        "pricing_model": "tiered",
        "pricing_config": {
            "input_field": "domains",
            "unit": "domain",
            "min_cents": 4,
            "tiers": [
                {"up_to_units": 1, "cents": 4},
                {"up_to_units": 3, "cents": 9},
                {"up_to_units": 10, "cents": 16},
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
