"""Read-only corpora used by parametrized surface tests.

# OWNS: route inventory, error-code list, job-state transitions, curated
#       built-in agent ids — anything tests/surface/ wants to parametrize over.
# NOT OWNS: hypothesis strategies (those live in tests/strategies.py).
# INVARIANTS: every public name here is a tuple/frozenset/list of immutables —
#             tests never mutate corpora.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=1)
def _load_error_codes() -> tuple[str, ...]:
    """Discover dot-namespaced lowercase error codes by walking core.error_codes attributes.

    Why: the taxonomy lives as module-level UPPER_CASE constants whose values
    are the canonical strings. Discovering at runtime keeps the corpus in
    sync if a new code is added without forcing every caller to maintain
    a parallel allowlist.
    """
    import core.error_codes as ec

    codes: set[str] = set()
    for name in dir(ec):
        if name.startswith("_") or name == "DEFAULT_BY_STATUS":
            continue
        val = getattr(ec, name)
        if not isinstance(val, str):
            continue
        if "." in val and " " not in val and val == val.lower():
            codes.add(val)
    return tuple(sorted(codes))


def error_codes() -> tuple[str, ...]:
    """Public accessor for the cached error-code corpus."""
    return _load_error_codes()


JOB_STATES: tuple[str, ...] = (
    "pending",
    "claimed",
    "running",
    "awaiting_clarification",
    "accepted",
    "complete",
    "expired",
    "failed",
    "cancelled",
)

JOB_EVENTS: tuple[str, ...] = (
    "claim",
    "heartbeat",
    "release",
    "clarification_request",
    "clarification_response",
    "progress",
    "complete",
    "fail",
    "cancel",
    "retry",
)

# Legal transitions: (from_state, event) -> to_state. Keep in sync with
# core.jobs.leases.py — the surface tests use this catalogue to assert the
# state machine is internally consistent.
LEGAL_TRANSITIONS: tuple[tuple[str, str, str], ...] = (
    ("pending", "claim", "claimed"),
    ("claimed", "heartbeat", "claimed"),
    ("claimed", "release", "pending"),
    ("claimed", "progress", "claimed"),
    ("claimed", "complete", "complete"),
    ("claimed", "fail", "failed"),
    ("claimed", "clarification_request", "awaiting_clarification"),
    ("awaiting_clarification", "clarification_response", "claimed"),
    ("awaiting_clarification", "cancel", "cancelled"),
    ("pending", "cancel", "cancelled"),
    ("claimed", "cancel", "cancelled"),
    ("running", "cancel", "cancelled"),
    ("failed", "retry", "pending"),
)


def illegal_transitions() -> Iterable[tuple[str, str]]:
    """Yield (state, event) pairs that should be rejected.

    Why: terminal states never accept further events. The matrix tests use
    this to drive parametrized illegal-transition assertions.
    """
    legal = {(s, e) for s, e, _ in LEGAL_TRANSITIONS}
    terminal = {"complete", "expired", "cancelled"}
    for s in JOB_STATES:
        for e in JOB_EVENTS:
            if (s, e) in legal:
                continue
            if s in terminal:
                yield (s, e)


@lru_cache(maxsize=1)
def curated_builtin_agent_ids() -> tuple[str, ...]:
    """Cached snapshot of the curated public built-in agent id set."""
    from server.builtin_agents.constants import CURATED_PUBLIC_BUILTIN_AGENT_IDS
    return tuple(CURATED_PUBLIC_BUILTIN_AGENT_IDS)


@lru_cache(maxsize=1)
def route_inventory() -> tuple[tuple[str, str, frozenset[str]], ...]:
    """List of (method, path, methods) for every route on `server.app`.

    Why: lazy introspection over `app.routes` keeps the catalogue in sync
    when routes are added; static excludes are limited to `/static` and
    `/_*` to keep the matrix tractable.
    """
    from server import app  # type: ignore[attr-defined]

    out: list[tuple[str, str, frozenset[str]]] = []
    for r in app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods:
            continue
        if path.startswith("/static") or path.startswith("/_"):
            continue
        for m in sorted(methods):
            if m in {"HEAD", "OPTIONS"}:
                continue
            out.append((m, path, frozenset(methods)))
    return tuple(sorted(set(out)))


OUTPUT_FORMATS: tuple[str, ...] = (
    "json",
    "markdown",
    "github_pr_comment",
    "slack_blocks",
    "text",
)
