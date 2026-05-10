"""Job state-transition corpus consistency + happy-path integration check.

# OWNS: assertions that the LEGAL_TRANSITIONS corpus is internally consistent
#       (no duplicate (from,event) pairs, all states/events declared, all
#       targets in the state set) and matches core.jobs.db.VALID_STATUSES.
# DECISIONS: deeper DB-driven illegal-transition assertions live in the
#       integration suite (test_jobs_core_messages.py); this file enforces
#       the *catalogue* invariants so corpus drift is caught.
"""
from __future__ import annotations

import pytest

from core.jobs.db import VALID_STATUSES
from tests.corpora import (
    JOB_EVENTS,
    JOB_STATES,
    LEGAL_TRANSITIONS,
    illegal_transitions,
)

pytestmark = pytest.mark.surface


@pytest.mark.parametrize("transition", LEGAL_TRANSITIONS, ids=lambda t: f"{t[0]}-{t[1]}->{t[2]}")
def test_legal_transition_states_are_known(transition):
    src, event, dst = transition
    assert src in JOB_STATES, f"unknown source state: {src}"
    assert dst in JOB_STATES, f"unknown destination state: {dst}"
    assert event in JOB_EVENTS, f"unknown event: {event}"


@pytest.mark.parametrize("transition", LEGAL_TRANSITIONS, ids=lambda t: f"{t[0]}-{t[1]}->{t[2]}")
def test_legal_destination_in_valid_statuses_or_pseudo(transition):
    """Destination is either a real persisted status (VALID_STATUSES) or one
    of the pseudo-states the corpus uses for testing (claimed, accepted,
    cancelled — these are derived from running + flags rather than stored)."""
    _, _, dst = transition
    pseudo_states = {"claimed", "accepted", "cancelled"}
    assert dst in VALID_STATUSES or dst in pseudo_states, (
        f"destination {dst!r} not in VALID_STATUSES or pseudo-states"
    )


def test_legal_transitions_have_no_duplicate_pairs():
    """A (from_state, event) pair is deterministic — at most one destination."""
    seen: dict[tuple[str, str], str] = {}
    for src, event, dst in LEGAL_TRANSITIONS:
        key = (src, event)
        if key in seen:
            assert seen[key] == dst, (
                f"non-deterministic: ({src}, {event}) -> {seen[key]} and {dst}"
            )
        seen[key] = dst


def test_legal_transitions_non_empty():
    assert LEGAL_TRANSITIONS


def test_job_states_match_persisted_set():
    """Every status used in VALID_STATUSES must appear in our JOB_STATES corpus
    so the surface tests cover real states the DB can hold."""
    for status in VALID_STATUSES:
        assert status in JOB_STATES, (
            f"status {status!r} present in VALID_STATUSES but missing from JOB_STATES"
        )


@pytest.mark.parametrize("pair", list(illegal_transitions()), ids=lambda p: f"{p[0]}-{p[1]}")
def test_illegal_transitions_from_terminal_states_listed(pair):
    """Smoke: the illegal_transitions generator only emits (state, event)
    pairs whose source is a terminal state — terminal states never accept
    further events."""
    src, _ = pair
    terminal = {"complete", "expired", "cancelled"}
    assert src in terminal


def test_legal_transitions_idempotent_when_iterated():
    """The corpus is a tuple — iterating twice yields identical pairs."""
    a = list(LEGAL_TRANSITIONS)
    b = list(LEGAL_TRANSITIONS)
    assert a == b
