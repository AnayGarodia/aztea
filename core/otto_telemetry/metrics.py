"""
metrics.py — section registry + dispatch for the /admin/otto dashboard.

One entry per dashboard tab. The route validates `section` against SECTIONS and
calls compute_section(section, window); each builder lives in a sections_* module
so no single file blows the line budget and each tab is independently testable.
"""

from __future__ import annotations

from typing import Any, Callable

from core.otto_telemetry import sections_growth as _growth
from core.otto_telemetry import sections_ops as _ops
from core.otto_telemetry import sections_quality as _quality
from core.otto_telemetry.queries import ALLOWED_WINDOWS, DEFAULT_WINDOW

# section name → builder(window) -> dict. Order here is the dashboard tab order.
_BUILDERS: dict[str, Callable[[str], dict[str, Any]]] = {
    "overview": _growth.overview,
    "growth": _growth.growth,
    "usage": _growth.usage,
    "quality": _quality.quality,
    "latency": _quality.latency,
    "matrix": _quality.matrix,
    "cost": _ops.cost,
    "reliability": _ops.reliability,
    "setup": _ops.setup,
    "learning": _ops.learning,
}

SECTIONS = tuple(_BUILDERS.keys())


def is_valid_window(window: str) -> bool:
    return window in ALLOWED_WINDOWS


def compute_section(section: str, window: str = DEFAULT_WINDOW) -> dict[str, Any]:
    """Compute one dashboard section. Raises KeyError for an unknown section so
    the route can map it to a 400 (silent empty would mask client typos)."""
    builder = _BUILDERS.get(section)
    if builder is None:
        raise KeyError(section)
    if not is_valid_window(window):
        window = DEFAULT_WINDOW
    return builder(window)


def compute_all(window: str = DEFAULT_WINDOW) -> dict[str, Any]:
    """Every section in one payload — used by the dashboard's initial load."""
    if not is_valid_window(window):
        window = DEFAULT_WINDOW
    return {name: builder(window) for name, builder in _BUILDERS.items()}
