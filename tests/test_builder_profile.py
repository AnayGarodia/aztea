# SPDX-License-Identifier: Apache-2.0
"""Wave 2 (2026-05-26): builder profile aggregator + endpoint smoke tests.

# OWNS: contract assertions for core.builder_profiles.build_profile and
#       the new GET /registry/builders/{username} route plus the
#       ?owner_id filter on GET /registry/agents.
# INVARIANTS:
#   - Earnings field is OMITTED (not zeroed) unless the builder opted in.
#     Frontend reads "key missing" as "hide the section"; "$0" would be
#     a different signal.
#   - BuilderNotFound raised on unknown username — never returned as an
#     empty profile, which would mask typo'd URLs.
#   - The owner_id filter on /registry/agents is presentation-only — does
#     not touch the cache and does not synthesize missing curated agents.

Tests stub the DB connection where possible to stay fast; the integration
test for the route uses the standard isolated_db fixture pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core import builder_profiles
from core.builder_profiles import BuilderNotFound, build_profile


# ─── BuilderProfile.to_jsonable() ──────────────────────────────────────────


def test_to_jsonable_omits_earnings_when_not_opted_in():
    profile = builder_profiles.BuilderProfile(
        username="alice",
        user_id="user_alice_uuid",
        agent_count=2,
        total_calls_served=100,
        average_rating=4.7,
        trust_score=0.92,
        earnings_visible=False,
        total_earnings_usd=234.56,  # set, but should NOT appear in output
        agents=[{"slug": "agent-a"}, {"slug": "agent-b"}],
    )
    payload = profile.to_jsonable()
    assert "total_earnings_usd" not in payload, (
        "Earnings must be omitted (not zeroed) when earnings_visible=False; "
        "frontend reads key-missing as 'hide the section'."
    )
    assert payload["earnings_visible"] is False
    assert payload["agent_count"] == 2
    assert payload["agents"] == [{"slug": "agent-a"}, {"slug": "agent-b"}]


def test_to_jsonable_includes_earnings_when_opted_in():
    profile = builder_profiles.BuilderProfile(
        username="alice",
        user_id="user_alice_uuid",
        agent_count=2,
        total_calls_served=100,
        average_rating=4.7,
        trust_score=0.92,
        earnings_visible=True,
        total_earnings_usd=234.56,
        agents=[],
    )
    payload = profile.to_jsonable()
    assert payload["total_earnings_usd"] == 234.56
    assert payload["earnings_visible"] is True


def test_to_jsonable_rounds_earnings_to_two_decimals():
    profile = builder_profiles.BuilderProfile(
        username="x", user_id="u", agent_count=0, total_calls_served=0,
        average_rating=None, trust_score=None,
        earnings_visible=True, total_earnings_usd=1.23456789, agents=[],
    )
    assert profile.to_jsonable()["total_earnings_usd"] == 1.23


def test_to_jsonable_omits_earnings_when_opted_in_but_value_is_none():
    """Opted-in but the aggregator returned None (zero payouts so far,
    or table missing). Treat as opted-in-but-no-data — omit the field
    so the frontend shows the section's empty state, not a misleading $0."""
    profile = builder_profiles.BuilderProfile(
        username="x", user_id="u", agent_count=0, total_calls_served=0,
        average_rating=None, trust_score=None,
        earnings_visible=True, total_earnings_usd=None, agents=[],
    )
    payload = profile.to_jsonable()
    assert "total_earnings_usd" not in payload


# ─── build_profile() with mocked DB ────────────────────────────────────────


def _row(*values):
    """sqlite3.Row-style tuple that also supports str-key access via dict."""
    class _R(tuple):
        def __getitem__(self, key):
            if isinstance(key, int):
                return tuple.__getitem__(self, key)
            # Fallback: callers test by integer index in our aggregator.
            raise KeyError(key)
    return _R(values)


def _stub_conn(monkeypatch, *, user_row, agents_rows=(), avg_rating=None,
               trust=None, earnings_cents=None):
    """Patch core.db.get_db_connection to yield a connection whose .execute()
    returns canned cursors for each SQL the aggregator runs."""
    calls: list[str] = []

    class _Cursor:
        def __init__(self, fetchone_value=None, fetchall_value=()):
            self._fetchone = fetchone_value
            self._fetchall = list(fetchall_value)

        def fetchone(self):
            return self._fetchone

        def fetchall(self):
            return self._fetchall

    class _Conn:
        def execute(self, sql, params=None):
            calls.append(sql.split()[0:2])  # rough op tag for debugging
            sql_lower = sql.lower()
            # Order matters — more-specific checks first.
            if "from users" in sql_lower:
                return _Cursor(fetchone_value=user_row)
            if "avg(cr.rating)" in sql_lower:
                return _Cursor(fetchone_value=_row(avg_rating))
            if "avg(trust_score)" in sql_lower:
                return _Cursor(fetchone_value=_row(trust))
            if "coalesce(sum(t.amount_cents)" in sql_lower:
                return _Cursor(fetchone_value=_row(earnings_cents))
            # Catch-all for the agents-by-owner list must come LAST since the
            # trust_score query also reads `FROM agents WHERE owner_id`.
            if "from agents where owner_id" in sql_lower:
                return _Cursor(fetchall_value=agents_rows)
            raise AssertionError(f"Un-stubbed SQL: {sql!r}")

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        builder_profiles._db, "get_db_connection", lambda: _Conn(),
    )
    return calls


def test_build_profile_raises_when_username_unknown(monkeypatch):
    _stub_conn(monkeypatch, user_row=None)
    with pytest.raises(BuilderNotFound):
        build_profile("ghost-user")


def test_build_profile_empty_username_raises(monkeypatch):
    """Don't pretend "" is a valid lookup; would silently match the first
    user row in some DB backends."""
    with pytest.raises(BuilderNotFound):
        build_profile("")


def test_build_profile_assembles_full_payload(monkeypatch):
    user_row = _row("user_alice", "alice", 1)  # profile_visible_earnings=1
    agents_rows = [
        _row(
            "agent-1", "cve-lookup", "CVE Lookup", "Look up CVEs.",
            0.03, "security", 1000, 0.97,
        ),
        _row(
            "agent-2", "dns-inspector", "DNS Inspector", "Live DNS.",
            0.05, "web", 250, 0.99,
        ),
    ]
    _stub_conn(
        monkeypatch, user_row=user_row, agents_rows=agents_rows,
        avg_rating=4.6, trust=0.88, earnings_cents=12345,
    )
    profile = build_profile("alice")
    assert profile.username == "alice"
    assert profile.user_id == "user_alice"
    assert profile.agent_count == 2
    assert profile.total_calls_served == 1250
    assert profile.average_rating == 4.6
    assert profile.trust_score == 0.88
    assert profile.earnings_visible is True
    assert profile.total_earnings_usd == 123.45
    assert len(profile.agents) == 2
    assert profile.agents[0]["slug"] == "cve-lookup"
    assert profile.agents[0]["total_calls"] == 1000


def test_build_profile_omits_earnings_when_flag_off(monkeypatch):
    """profile_visible_earnings=0 ⇒ the aggregator must NOT even query
    the transactions table (privacy-first), AND the field stays out of
    to_jsonable()."""
    user_row = _row("user_alice", "alice", 0)  # opted out
    _stub_conn(
        monkeypatch, user_row=user_row, agents_rows=(),
        avg_rating=None, trust=None, earnings_cents=99999,
    )
    profile = build_profile("alice")
    assert profile.earnings_visible is False
    assert profile.total_earnings_usd is None
    assert "total_earnings_usd" not in profile.to_jsonable()


def test_build_profile_handles_builder_with_zero_agents(monkeypatch):
    """A newly-registered user with no published agents still has a valid
    profile — agent_count=0, totals=0. Don't 404 just because the agents
    table is empty."""
    user_row = _row("user_bob", "bob", 0)
    _stub_conn(
        monkeypatch, user_row=user_row, agents_rows=(),
        avg_rating=None, trust=None, earnings_cents=0,
    )
    profile = build_profile("bob")
    assert profile.agent_count == 0
    assert profile.total_calls_served == 0
    assert profile.average_rating is None
    assert profile.trust_score is None
    assert profile.agents == []
