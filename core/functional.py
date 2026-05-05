"""
Functional programming utilities for aztea.

# OWNS: Result monad, pipe/compose helpers, pure-function patterns
# NOT OWNS: business logic, DB access, HTTP layer
# INVARIANTS:
#   - All types here are immutable (frozen dataclasses or NamedTuple)
#   - No I/O in this module — pure computation only
#   - Ok/Err are structurally typed; callers pattern-match on .ok
# DECISIONS:
#   - Result uses a dataclass rather than Exception-based control flow so
#     error paths are explicit in type signatures and pipelines compose cleanly
#   - pipe() is eager (not lazy) — latency is not a concern at our scale

from __future__ import annotations
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

A = TypeVar("A")
B = TypeVar("B")
E = TypeVar("E")


# ---------------------------------------------------------------------------
# Result monad — explicit Ok / Err instead of exceptions in pure functions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ok(Generic[A]):
    """Successful result wrapping a value."""

    value: A
    ok: bool = True

    def map(self, f: Callable[[A], B]) -> "Ok[B]":
        return Ok(f(self.value))

    def map_err(self, f: Callable) -> "Ok[A]":
        return self

    def and_then(self, f: Callable[[A], "Result"]) -> "Result":
        return f(self.value)

    def unwrap(self) -> A:
        return self.value

    def unwrap_or(self, default: A) -> A:  # type: ignore[misc]
        return self.value

    def raise_on_err(self) -> A:
        """Return the wrapped value. Ok variant is a no-op pass-through."""
        return self.value

    def __bool__(self) -> bool:
        return True


@dataclass(frozen=True)
class Err(Generic[E]):
    """Failed result wrapping an error."""

    error: E
    ok: bool = False

    def map(self, f: Callable) -> "Err[E]":
        return self

    def map_err(self, f: Callable[[E], B]) -> "Err[B]":
        return Err(f(self.error))

    def and_then(self, f: Callable) -> "Err[E]":
        return self

    def unwrap(self) -> None:
        raise ValueError(f"Called unwrap() on Err: {self.error!r}")

    def unwrap_or(self, default: A) -> A:
        return default

    def raise_on_err(self) -> None:
        """Raise ValueError(error) directly — cleaner than isinstance checks at call sites."""
        raise ValueError(self.error)

    def __bool__(self) -> bool:
        return False


Result = Ok[A] | Err[E]


# ---------------------------------------------------------------------------
# Pipeline composition — left-to-right function chaining
# ---------------------------------------------------------------------------


def pipe(value: A, *fns: Callable) -> object:
    """Apply functions left-to-right: pipe(x, f, g, h) == h(g(f(x))).

    Each function receives the output of the previous one.
    Stops early if a Result becomes Err (short-circuit semantics).

    Example:
        result = pipe(
            {"price_cents": 100},
            validate_price,
            apply_fee,
            compute_payout,
        )
    """
    acc = value
    for fn in fns:
        if isinstance(acc, Err):
            return acc
        acc = fn(acc)
    return acc


def compose(*fns: Callable) -> Callable:
    """Return a function that applies fns right-to-left: compose(f, g)(x) == f(g(x))."""
    def composed(value: object) -> object:
        acc = value
        for fn in reversed(fns):
            acc = fn(acc)
        return acc
    return composed


def pipeline(*fns: Callable) -> Callable:
    """Return a function that applies fns left-to-right: pipeline(f, g)(x) == g(f(x))."""
    def run(value: object) -> object:
        return pipe(value, *fns)
    return run


# ---------------------------------------------------------------------------
# Validation helpers — return Result instead of raising
# ---------------------------------------------------------------------------


def validate(value: A, predicate: Callable[[A], bool], error: E) -> Result:
    """Return Ok(value) if predicate(value) is True, else Err(error)."""
    return Ok(value) if predicate(value) else Err(error)


def require_positive_int(value: object, field: str) -> Result:
    """Validate that value is a positive integer (common money-path check)."""
    if not isinstance(value, int) or isinstance(value, bool):
        return Err(f"{field} must be an integer, got {type(value).__name__}")
    if value <= 0:
        return Err(f"{field} must be positive, got {value}")
    return Ok(value)


def require_non_negative_int(value: object, field: str) -> Result:
    """Validate that value is a non-negative integer."""
    if not isinstance(value, int) or isinstance(value, bool):
        return Err(f"{field} must be an integer, got {type(value).__name__}")
    if value < 0:
        return Err(f"{field} must be non-negative, got {value}")
    return Ok(value)


# ---------------------------------------------------------------------------
# Pure computation helpers for settlement math
# ---------------------------------------------------------------------------


def compute_platform_fee(price_cents: int, fee_pct: int) -> int:
    """Return the platform fee in cents (integer, truncated). Pure function."""
    return price_cents * fee_pct // 100


def compute_agent_payout(price_cents: int, fee_pct: int) -> int:
    """Return the agent payout after platform fee. Pure function."""
    return price_cents - compute_platform_fee(price_cents, fee_pct)


def compute_caller_refund(caller_charge_cents: int, price_cents: int) -> int:
    """Return the refund owed to the caller on failure (full charge). Pure function."""
    return caller_charge_cents


def compute_partial_refund(
    caller_charge_cents: int, price_cents: int, quality_pct: int
) -> int:
    """Compute partial refund for quality-adjusted payouts. Pure function.

    quality_pct: 0–100, where 100 means full payout (no refund) and 0 means
    full refund to caller.
    """
    effective_price = price_cents * max(0, min(100, quality_pct)) // 100
    overpaid = caller_charge_cents - effective_price
    return max(0, overpaid)


def compute_clawback_cents(agent_payout_cents: int, payout_fraction: float) -> int:
    """Return the clawback amount owed to the caller when payout_fraction < 1.

    Uses Decimal ROUND_HALF_UP — the same rounding used in payout_curve.py —
    so callers can replace inline Decimal math with this pure function.
    payout_fraction is clamped to [0.0, 1.0].
    """
    from decimal import ROUND_HALF_UP, Decimal

    clamped = max(0.0, min(1.0, float(payout_fraction)))
    if clamped >= 1.0:
        return 0
    clawback = (
        Decimal(str(agent_payout_cents)) * (Decimal("1") - Decimal(str(clamped)))
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return max(0, int(clawback))
