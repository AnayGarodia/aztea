"""Stop_when predicate validation + bounded JMESPath evaluation.

# OWNS: parsing, validating, and safely evaluating user-supplied JMESPath
#   stop_when predicates against partial_output payloads. Caps cardinality
#   and cost so a malicious or sloppy predicate cannot stall the messaging
#   transaction.
# NOT OWNS: state transitions, settlement, or message persistence — this
#   module is pure validation + eval. Callers (messaging.py, route handlers)
#   own side effects.
# INVARIANTS:
#   - At most STOP_WHEN_MAX_PREDICATES predicates per job.
#   - Each expr <= STOP_WHEN_MAX_EXPR_LEN characters.
#   - No wildcard projection deeper than STOP_WHEN_MAX_PROJECTION_DEPTH.
#   - Per-predicate eval budget STOP_WHEN_EVAL_BUDGET_S; on timeout the
#     predicate is skipped for this partial only and the timeout counter
#     is incremented.
# DECISIONS:
#   - Recompile per evaluation rather than caching. JMESPath compile is
#     microseconds; cross-process cache is premature.
#   - Thread-pool timeout (concurrent.futures) is the simplest portable
#     way to interrupt JMESPath; we accept the small thread-creation cost
#     because partials arrive at human pace.
#   - "Match" = predicate returns truthy (non-empty list, non-zero number,
#     non-empty string, true). None / [] / "" / 0 / False = no match.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

import jmespath
from jmespath import exceptions as jmespath_exceptions

_LOG = logging.getLogger(__name__)

STOP_WHEN_MAX_PREDICATES = 8
STOP_WHEN_MAX_EXPR_LEN = 500
STOP_WHEN_MAX_LABEL_LEN = 64
STOP_WHEN_MAX_PROJECTION_DEPTH = 2
STOP_WHEN_MAX_OR_CHAIN = 4
STOP_WHEN_EVAL_BUDGET_S = 0.025

# Singleton executor — reusing a small pool is cheaper than spawning a fresh
# one per partial. Bounded to a small worker count because the per-eval budget
# is tiny.
_EVAL_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stopwhen")


class StopWhenInvalid(ValueError):
    """Raised at submit time when a predicate fails validation."""


def validate_stop_when(raw: list[dict] | None) -> list[dict]:
    """Validate a stop_when array submitted at job creation.

    Returns a list of normalized {label, expr} dicts. Each predicate is
    parsed-checked and complexity-bounded. Raises StopWhenInvalid with the
    offending label / expr on the first failure.

    A None / empty input returns []; the caller decides whether to store an
    empty array or None.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise StopWhenInvalid("stop_when must be a list of {label, expr} objects")
    if len(raw) > STOP_WHEN_MAX_PREDICATES:
        raise StopWhenInvalid(
            f"stop_when exceeds {STOP_WHEN_MAX_PREDICATES} predicates"
        )

    seen_labels: set[str] = set()
    out: list[dict] = []
    for idx, item in enumerate(raw):
        normalized = _validate_one_predicate(idx, item, seen_labels)
        seen_labels.add(normalized["label"])
        out.append(normalized)
    return out


def _validate_one_predicate(
    idx: int, item: object, seen_labels: set[str]
) -> dict:
    """Validate a single {label, expr} entry; return the normalized dict.

    Split out from validate_stop_when so the outer function stays under
    the cyclomatic-complexity budget. Raises StopWhenInvalid on the first
    failure; the offending label / index is included so the API caller
    can fix it without a second round-trip.
    """
    if not isinstance(item, dict):
        raise StopWhenInvalid(f"stop_when[{idx}] must be an object")
    label = str(item.get("label") or "").strip()
    expr = str(item.get("expr") or "").strip()
    if not label:
        raise StopWhenInvalid(f"stop_when[{idx}].label is required")
    if len(label) > STOP_WHEN_MAX_LABEL_LEN:
        raise StopWhenInvalid(
            f"stop_when[{idx}].label exceeds {STOP_WHEN_MAX_LABEL_LEN} chars"
        )
    if label in seen_labels:
        raise StopWhenInvalid(f"stop_when label {label!r} is duplicated")
    if not expr:
        raise StopWhenInvalid(f"stop_when[{label}].expr is required")
    if len(expr) > STOP_WHEN_MAX_EXPR_LEN:
        raise StopWhenInvalid(
            f"stop_when[{label}].expr exceeds {STOP_WHEN_MAX_EXPR_LEN} chars"
        )
    try:
        compiled = jmespath.compile(expr)
    except jmespath_exceptions.ParseError as exc:
        raise StopWhenInvalid(
            f"stop_when[{label}].expr is not valid JMESPath: {exc}"
        ) from exc
    _check_complexity(compiled.parsed, label=label)
    return {"label": label, "expr": expr}


def _check_complexity(node: dict, *, label: str) -> None:
    """Walk the parsed JMESPath AST and reject pathological shapes.

    The bounds are intentionally conservative: we want most ergonomic
    predicates to pass and only block expressions that could blow up eval
    time on large payloads.
    """
    proj_depth, or_count = _walk_ast(node)
    if proj_depth > STOP_WHEN_MAX_PROJECTION_DEPTH:
        raise StopWhenInvalid(
            f"stop_when[{label}] uses wildcard projection deeper than "
            f"{STOP_WHEN_MAX_PROJECTION_DEPTH} levels"
        )
    if or_count > STOP_WHEN_MAX_OR_CHAIN:
        raise StopWhenInvalid(
            f"stop_when[{label}] uses more than {STOP_WHEN_MAX_OR_CHAIN} "
            f"|| operators"
        )


_PROJECTION_NODE_TYPES = {
    "projection",
    "object_projection",
    "filter_projection",
    "slice",
}


def _walk_ast(node: Any) -> tuple[int, int]:
    """Return (max_projection_depth, or_count) for a parsed JMESPath AST.

    JMESPath's parsed form is a dict with 'type' and 'children'. We walk
    iteratively and track the maximum nested-projection depth seen on any
    path from the root. ``or`` operators are counted globally (across all
    branches) since the cost is roughly additive.
    """
    if not isinstance(node, dict):
        return (0, 0)
    max_depth = 0
    or_count = 0
    # Stack holds (subtree, depth-so-far).
    stack: list[tuple[dict, int]] = [(node, 0)]
    while stack:
        cur, depth = stack.pop()
        ntype = cur.get("type")
        new_depth = depth + 1 if ntype in _PROJECTION_NODE_TYPES else depth
        if new_depth > max_depth:
            max_depth = new_depth
        if ntype == "or_expression":
            or_count += 1
        for child in cur.get("children", []) or []:
            if isinstance(child, dict):
                stack.append((child, new_depth))
    return max_depth, or_count


def evaluate_first_match(
    predicates: list[dict],
    payload: Any,
) -> dict | None:
    """Evaluate predicates against ``payload`` and return the first match.

    Each predicate runs under a STOP_WHEN_EVAL_BUDGET_S wallclock budget.
    Predicates that time out are skipped for this payload (and logged); they
    do not abort the stream. A predicate matches when its result is truthy.

    Returns the matched {label, expr} dict, or None if none matched.
    """
    for pred in predicates or []:
        if not isinstance(pred, dict):
            continue
        label = pred.get("label")
        expr = pred.get("expr")
        if not (isinstance(label, str) and isinstance(expr, str)):
            continue
        try:
            result = _evaluate_one(expr, payload)
        except _PredicateTimeout:
            _LOG.warning(
                "stop_when.eval_timeout",
                extra={"label": label, "expr_len": len(expr)},
            )
            continue
        except Exception as exc:
            # JMESPath errors at runtime (e.g. type errors) — log and skip
            # this predicate for this payload. Other predicates may still
            # match.
            _LOG.warning(
                "stop_when.eval_error",
                extra={"label": label, "error": str(exc)[:200]},
            )
            continue
        if _is_truthy(result):
            return {"label": label, "expr": expr}
    return None


class _PredicateTimeout(RuntimeError):
    pass


def _evaluate_one(expr: str, payload: Any) -> Any:
    """Run a single JMESPath expression with a wallclock budget.

    Compiles fresh each call (compile is microseconds) and submits to the
    shared executor. On timeout, raises _PredicateTimeout — note we cannot
    actually interrupt the JMESPath thread; the work continues until done
    or the process exits, but our caller has already moved on.
    """
    compiled = jmespath.compile(expr)
    future = _EVAL_POOL.submit(compiled.search, payload)
    try:
        return future.result(timeout=STOP_WHEN_EVAL_BUDGET_S)
    except FuturesTimeoutError as exc:
        raise _PredicateTimeout() from exc


def _is_truthy(value: Any) -> bool:
    """JMESPath-style truthiness: None / [] / {} / "" / 0 / False are falsy."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return bool(value)
