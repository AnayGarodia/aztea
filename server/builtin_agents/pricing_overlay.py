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
    IMAGE_GENERATOR_AGENT_ID,
    VIDEO_STORYBOARD_AGENT_ID,
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
}


def get_pricing_overlay() -> dict[str, dict[str, Any]]:
    """Return a shallow copy of the overlay for consumption by part_008."""
    return {k: dict(v) for k, v in _OVERLAY.items()}
