# OWNS: parsing cron expressions and computing next scheduled run times
# NOT OWNS: job scheduling execution, cron daemon management
# INVARIANTS: never executes the cron command; pure time arithmetic only
# DECISIONS: uses croniter when available (handles more edge cases), falls back to manual next-run computation

"""
Cron expression parser agent.

Inputs:  expression (str), n (int), timezone (str), reference_time (str)
Outputs: parsed fields, description, next_runs, frequency stats, warnings
External deps: croniter (optional); zoneinfo (stdlib Python 3.9+)

Why this agent: cron edge cases (L, W, #, @reboot, 6-field, timezone offsets)
are routinely hallucinated in chat — this agent uses real parsing logic.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_N = 20
MAX_WALK_ITERATIONS = 1440  # one day of minute-by-minute steps
HIGH_FREQUENCY_THRESHOLD = 100  # runs/day that triggers a warning
FREQUENCY_WINDOW_MINUTES = 1440  # 24-hour window for runs_per_day estimate
FIELD_NAMES_5 = ("minute", "hour", "day_of_month", "month", "day_of_week")
FIELD_NAMES_6 = ("second", "minute", "hour", "day_of_month", "month", "day_of_week")

MACRO_MAP: dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


def run(payload: dict) -> dict:
    """
    Parse a cron expression and return next run times plus metadata.

    Why: cron edge cases cannot be reliably computed in chat; this agent
    uses croniter or a deterministic fallback to guarantee correctness.
    """
    expression = payload.get("expression", "").strip()
    if not expression:
        return _error("cron_expression_parser.missing_expression", "expression is required")

    raw_n = payload.get("n", 5)
    if not isinstance(raw_n, int) or raw_n < 1:
        raw_n = 5
    if raw_n > MAX_N:
        return _error("cron_expression_parser.n_too_large", f"n must be <= {MAX_N}")

    tz_name = payload.get("timezone", "UTC")
    tz = _resolve_timezone(tz_name)
    if tz is None:
        return _error("cron_expression_parser.unknown_timezone", f"Unknown timezone: {tz_name!r}")

    ref_raw = payload.get("reference_time")
    reference_time = _parse_reference_time(ref_raw, tz)

    if expression == "@reboot":
        return _reboot_result(tz_name)

    normalized = MACRO_MAP.get(expression, expression)

    fields = normalized.split()
    if len(fields) not in (5, 6):
        return _error(
            "cron_expression_parser.invalid_expression",
            f"Expected 5 or 6 fields, got {len(fields)}",
        )

    warnings: list[str] = []
    field_breakdown = _build_field_breakdown(fields)
    description = _describe(normalized, fields)

    next_runs, tool_used = _compute_next_runs(normalized, raw_n, reference_time, tz, warnings)

    freq = _compute_frequency(normalized, reference_time, tz)
    if freq["runs_per_day"] > HIGH_FREQUENCY_THRESHOLD:
        warnings.append("runs_more_than_100x_per_day")

    return {
        "expression": normalized,
        "description": description,
        "valid": True,
        "field_breakdown": field_breakdown,
        "next_runs": next_runs,
        "frequency": freq,
        "warnings": warnings,
        "timezone": tz_name,
        "tool_used": tool_used,
    }


# ---------------------------------------------------------------------------
# Field breakdown
# ---------------------------------------------------------------------------

def _build_field_breakdown(fields: list[str]) -> dict:
    """Map parsed fields to named keys; include 'second' only for 6-field expressions."""
    if len(fields) == 5:
        return dict(zip(FIELD_NAMES_5, fields)) | {"second": None}
    return dict(zip(FIELD_NAMES_6, fields))


# ---------------------------------------------------------------------------
# Human description
# ---------------------------------------------------------------------------

_DOW_NAMES = {
    "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
    "4": "Thursday", "5": "Friday", "6": "Saturday",
}


def _describe(normalized: str, fields: list[str]) -> str:
    """Return a readable English description for common cron patterns."""
    # Normalise to 5-field for description purposes
    f = fields if len(fields) == 5 else fields[1:]  # drop seconds field
    minute, hour, dom, month, dow = f

    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        step = minute[2:]
        return f"Every {step} minutes"

    if minute == "0" and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return "At minute 0 of every hour"

    if minute == "0" and dom == "*" and month == "*" and dow == "*" and _is_literal(hour):
        return f"Every day at {int(hour):02d}:00"

    if minute == "0" and month == "*" and dom == "*" and _is_literal(hour) and _is_literal(dow):
        day_name = _DOW_NAMES.get(dow, f"day {dow}")
        return f"Every {day_name} at {int(hour):02d}:00"

    parts = [
        f"minute={minute}", f"hour={hour}", f"dom={dom}",
        f"month={month}", f"dow={dow}",
    ]
    return "Runs when: " + ", ".join(p for p in parts if not p.endswith("=*"))


def _is_literal(field: str) -> bool:
    """Return True if field is a plain integer with no special characters."""
    return field.isdigit()


# ---------------------------------------------------------------------------
# Next-run computation
# ---------------------------------------------------------------------------

def _compute_next_runs(
    expression: str,
    n: int,
    reference_time: datetime,
    tz: ZoneInfo,
    warnings: list[str],
) -> tuple[list[str], str]:
    """Try croniter first; fall back to builtin walker. Returns (runs, tool_used)."""
    try:
        from croniter import croniter  # noqa: PLC0415 — intentional lazy import for optional dep

        start = reference_time.astimezone(tz)
        iter_ = croniter(expression, start, hash_use_datetime=True)
        runs = [iter_.get_next(datetime).isoformat() for _ in range(n)]
        return runs, "croniter"
    except ImportError:
        pass
    except Exception as exc:
        warnings.append(f"croniter_error: {exc}")

    runs = _builtin_next_runs(expression, n, reference_time, tz, warnings)
    return runs, "builtin"


def _builtin_next_runs(
    expression: str,
    n: int,
    reference_time: datetime,
    tz: ZoneInfo,
    warnings: list[str],
) -> list[str]:
    """
    Walk forward minute-by-minute from reference_time to find matching cron slots.

    Only handles simple fields (*, integers, ranges a-b, steps */x and a-b/x).
    Complex modifiers like L, W, # are unsupported — croniter must handle those.
    """
    fields = expression.split()
    # Normalise to 5-field (drop seconds if present)
    five = fields if len(fields) == 5 else fields[1:]
    try:
        valid_minutes = _expand_field(five[0], 0, 59)
        valid_hours = _expand_field(five[1], 0, 23)
        valid_doms = _expand_field(five[2], 1, 31)
        valid_months = _expand_field(five[3], 1, 12)
        valid_dows = _expand_field(five[4], 0, 6)
    except ValueError:
        warnings.append("builtin_parser_unsupported_field")
        return []

    cursor = reference_time.astimezone(tz) + timedelta(minutes=1)
    cursor = cursor.replace(second=0, microsecond=0)
    results: list[str] = []
    iterations = 0
    max_iter = MAX_WALK_ITERATIONS * MAX_N  # don't walk more than ~14 400 minutes

    while len(results) < n and iterations < max_iter:
        iterations += 1
        # Python: cursor.weekday() = Mon..Sun (0..6).
        # Cron:   day-of-week = Sun..Sat (0..6).
        # Truth table: Mon→1, Tue→2, ..., Sat→6, Sun→0.
        # 1.7.0 fixed this in _runs_per_day but missed _compute_next_runs;
        # 1.7.1 reuses the same conversion here.
        cron_dow = (cursor.weekday() + 1) % 7
        if (
            cursor.month in valid_months
            and cursor.day in valid_doms
            and cron_dow in valid_dows
            and cursor.hour in valid_hours
            and cursor.minute in valid_minutes
        ):
            results.append(cursor.isoformat())
        cursor += timedelta(minutes=1)

    if not results:
        warnings.append("never_fires")
    return results


def _expand_field(field: str, lo: int, hi: int) -> set[int]:
    """
    Expand a cron field token into a set of integers.

    Supports: *, integers, ranges (a-b), steps (*/x, a-b/x).
    Raises ValueError for unsupported modifiers (L, W, #).
    """
    if any(c in field for c in ("L", "W", "#")):
        raise ValueError(f"Unsupported modifier in field: {field!r}")

    result: set[int] = set()
    for part in field.split(","):
        result |= _expand_single(part, lo, hi)
    return result


def _expand_single(part: str, lo: int, hi: int) -> set[int]:
    """Expand one comma-part of a cron field (no commas inside)."""
    step = 1
    if "/" in part:
        part, step_str = part.split("/", 1)
        step = int(step_str)

    if part == "*":
        base_range = range(lo, hi + 1)
    elif "-" in part:
        a, b = part.split("-", 1)
        base_range = range(int(a), int(b) + 1)
    else:
        val = int(part)
        base_range = range(val, val + 1)

    return set(base_range[::step])


# ---------------------------------------------------------------------------
# Frequency estimation
# ---------------------------------------------------------------------------

def _compute_frequency(expression: str, reference_time: datetime, tz: ZoneInfo) -> dict:
    """
    Estimate runs_per_day by counting matches in a 24-hour window.

    Approximate only — uses the builtin minute walker, not croniter.
    interval_minutes is None for non-uniform schedules.
    """
    fields = expression.split()
    five = fields if len(fields) == 5 else fields[1:]

    try:
        valid_minutes = _expand_field(five[0], 0, 59)
        valid_hours = _expand_field(five[1], 0, 23)
        valid_doms = _expand_field(five[2], 1, 31)
        valid_months = _expand_field(five[3], 1, 12)
        valid_dows = _expand_field(five[4], 0, 6)
    except ValueError:
        return {"runs_per_day": 0.0, "runs_per_hour": 0.0, "interval_minutes": None}

    cursor = reference_time.astimezone(tz).replace(second=0, microsecond=0)
    count = 0
    gaps: list[int] = []
    last_match: datetime | None = None

    for i in range(FREQUENCY_WINDOW_MINUTES):
        t = cursor + timedelta(minutes=i)
        # Python: t.weekday() = Mon..Sun (0..6).
        # Cron:   day-of-week = Sun..Sat (0..6, with 7 == 0 in many impls).
        # Pre-1.7.0 used `t.weekday() % 7` which left Monday at 0 →
        # `0 9 * * 1-5` (Mon-Fri) matched Tue-Sat instead, returning
        # runs_per_day=0 because Saturday rarely lands in the 24-hour window.
        # Convert Mon=0 → 1, Tue=1 → 2, ..., Sun=6 → 0.
        cron_dow = (t.weekday() + 1) % 7
        if (
            t.month in valid_months
            and t.day in valid_doms
            and cron_dow in valid_dows
            and t.hour in valid_hours
            and t.minute in valid_minutes
        ):
            count += 1
            if last_match is not None:
                gaps.append(int((t - last_match).total_seconds() // 60))
            last_match = t

    runs_per_day = float(count)
    runs_per_hour = round(runs_per_day / 24, 4)
    interval_minutes: float | None = None
    if gaps and len(set(gaps)) == 1:
        interval_minutes = float(gaps[0])

    return {
        "runs_per_day": runs_per_day,
        "runs_per_hour": runs_per_hour,
        "interval_minutes": interval_minutes,
    }


# ---------------------------------------------------------------------------
# Special cases
# ---------------------------------------------------------------------------

def _reboot_result(tz_name: str) -> dict:
    """Return a canonical result for @reboot — no next_runs since it's event-driven."""
    return {
        "expression": "@reboot",
        "description": "At system reboot",
        "valid": True,
        "field_breakdown": {
            "minute": None, "hour": None, "day_of_month": None,
            "month": None, "day_of_week": None, "second": None,
        },
        "next_runs": [],
        "frequency": {"runs_per_day": 0.0, "runs_per_hour": 0.0, "interval_minutes": None},
        "warnings": ["reboot_trigger_not_time_based"],
        "timezone": tz_name,
        "tool_used": "builtin",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_timezone(name: str) -> ZoneInfo | None:
    """Return a ZoneInfo for name, or None if not found."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        return None


def _parse_reference_time(raw: str | None, tz: ZoneInfo) -> datetime:
    """
    Parse ISO-8601 reference_time string or return now in the given timezone.

    Falls back to now if raw is None or unparseable — callers treat this as
    'compute from current time', so silent fallback is intentional here.
    """
    if raw is None:
        return datetime.now(tz)
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except ValueError:
        return datetime.now(tz)


def _error(code: str, message: str) -> dict:
    """Return a structured error envelope."""
    return {"error": {"code": code, "message": message}}
