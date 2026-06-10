"""Validate-before-replay freshness for cached maps and API specs.

# OWNS: the decision of whether a stored map/spec may be replayed without a
#        full re-derive (the Stagehand "validate the page still matches before
#        you trust the cached selector" rule).
# NOT OWNS: the actual browser render / HTTP fetch (injected as callbacks so
#           this module stays pure and unit-testable), DB writes.
# INVARIANTS:
#   * Never report fresh on a non-active row.
#   * A fingerprint mismatch is always 'drift' (caller must re-derive + the
#     store demotes via drift_count) — accuracy over hit-rate.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.site_maps import normalize as _normalize

_DEFAULT_TTL_HOURS = 24
_TTL_MIN_HOURS = 1
_TTL_MAX_HOURS = 168  # 7 days, matching the result cache ceiling


def _parse_iso(ts: str | None) -> float | None:
    """Delegate to the shared, tz-safe parser (one impl in normalize.py)."""
    return _normalize.parse_iso_to_epoch(ts)


def clamp_ttl_hours(ttl_hours: int | None) -> int:
    """Pure: bound the caller's freshness window to [1, 168] h."""
    try:
        value = int(ttl_hours if ttl_hours is not None else _DEFAULT_TTL_HOURS)
    except (TypeError, ValueError):
        value = _DEFAULT_TTL_HOURS
    return max(_TTL_MIN_HOURS, min(value, _TTL_MAX_HOURS))


def is_within_ttl(map_row: dict[str, Any], ttl_hours: int, now_epoch: float) -> bool:
    """Pure: True if the row was validated/created within the TTL window."""
    when = _parse_iso(map_row.get("last_validated_at")) or _parse_iso(map_row.get("created_at"))
    if when is None:
        return False
    return (now_epoch - when) <= clamp_ttl_hours(ttl_hours) * 3600.0


def validate_map_before_replay(
    map_row: dict[str, Any],
    *,
    recompute_fingerprint: Callable[[], str],
    ttl_hours: int = _DEFAULT_TTL_HOURS,
    now_epoch: float | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Decide whether ``map_row`` can be replayed. Returns (is_fresh, reason).

    Within TTL → trust the stored fingerprint (cheap, no recompute). Past TTL or
    forced → call ``recompute_fingerprint`` and compare; match renews, mismatch
    is drift. ``recompute_fingerprint`` is the (side-effecting) render/hash the
    caller supplies; kept out of this module so the policy stays pure.
    """
    now = now_epoch if now_epoch is not None else time.time()
    if str(map_row.get("status") or "") != "active":
        return False, "inactive"
    if not force and is_within_ttl(map_row, ttl_hours, now):
        return True, "within_ttl"
    current = recompute_fingerprint()
    if current == str(map_row.get("dom_fingerprint") or ""):
        return True, "revalidated"
    return False, "drift"


def validate_api_spec_before_replay(
    spec_row: dict[str, Any],
    *,
    recompute_response_fingerprint: Callable[[], str],
    ttl_hours: int = _DEFAULT_TTL_HOURS,
    now_epoch: float | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Same policy as maps but over the response-shape fingerprint of an API spec."""
    now = now_epoch if now_epoch is not None else time.time()
    if str(spec_row.get("status") or "") != "active":
        return False, "inactive"
    if not force and is_within_ttl(spec_row, ttl_hours, now):
        return True, "within_ttl"
    current = recompute_response_fingerprint()
    if current == str(spec_row.get("response_fingerprint") or ""):
        return True, "revalidated"
    return False, "drift"
