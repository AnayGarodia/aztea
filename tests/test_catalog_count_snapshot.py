"""Golden-file snapshot of the curated built-in agent catalog.

This test exists for one reason: when someone adds or removes a curated
agent, the catalog count is documentation-bearing (it's quoted in
README.md, CLAUDE.md, docs/oss-vs-hosted.md, and the homepage hero copy).
A silent drift between the constants file and the docs is the bug class
this snapshot prevents.

Updating this test is part of the same PR that changes the catalog —
not a follow-up, not a chore, not deferred. If you're here because this
test failed, you also need to update:

  * server/builtin_agents/constants.py (the change you're making)
  * README.md ("N built-in specialists")
  * CLAUDE.md (lines 345 and 623 today, grep for "curated public")
  * docs/oss-vs-hosted.md ("all N curated")
  * frontend/src/seo/copy.js (homepage hero quotes the number)
"""

from __future__ import annotations

from server.builtin_agents.constants import (
    ACCESSIBILITY_AUDITOR_AGENT_ID,
    BROWSER_AGENT_ID,
    CURATED_PUBLIC_BUILTIN_AGENT_IDS,
    CVELOOKUP_AGENT_ID,
    DB_SANDBOX_AGENT_ID,
    DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    LIGHTHOUSE_AUDITOR_AGENT_ID,
    LIVE_SANDBOX_AGENT_ID,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    SITE_NAVIGATOR_AGENT_ID,
    SUNSET_DEPRECATED_AGENT_IDS,
)

# WHY: post-2026-05-26 platform-pivot cull (10) + 2026-06-01 site_navigator, the
# agent-readable-web magnet, added to the curated set (→11). See the comment block
# above SUNSET_DEPRECATED_AGENT_IDS in constants.py for the per-agent reasoning.
_CURATED_EXPECTED = frozenset({
    CVELOOKUP_AGENT_ID,
    DEPENDENCY_AUDITOR_AGENT_ID,
    DNS_INSPECTOR_AGENT_ID,
    PYTHON_EXECUTOR_AGENT_ID,
    MULTI_LANGUAGE_EXECUTOR_AGENT_ID,
    LIVE_SANDBOX_AGENT_ID,
    DB_SANDBOX_AGENT_ID,
    BROWSER_AGENT_ID,
    LIGHTHOUSE_AUDITOR_AGENT_ID,
    ACCESSIBILITY_AUDITOR_AGENT_ID,
    SITE_NAVIGATOR_AGENT_ID,
})

_CURATED_EXPECTED_COUNT = 11


def test_curated_catalog_count_matches_snapshot() -> None:
    actual = len(CURATED_PUBLIC_BUILTIN_AGENT_IDS)
    assert actual == _CURATED_EXPECTED_COUNT, (
        f"Curated catalog count changed: expected {_CURATED_EXPECTED_COUNT}, "
        f"got {actual}. Update tests/test_catalog_count_snapshot.py AND every "
        f"doc that quotes the number (see this file's docstring)."
    )


def test_curated_catalog_membership_matches_snapshot() -> None:
    added = CURATED_PUBLIC_BUILTIN_AGENT_IDS - _CURATED_EXPECTED
    removed = _CURATED_EXPECTED - CURATED_PUBLIC_BUILTIN_AGENT_IDS
    assert not added and not removed, (
        f"Curated catalog membership drifted from snapshot.\n"
        f"  added (in constants but not snapshot): {sorted(added)}\n"
        f"  removed (in snapshot but not constants): {sorted(removed)}\n"
        f"Update _CURATED_EXPECTED in tests/test_catalog_count_snapshot.py "
        f"AND the docs (see this file's docstring)."
    )


def test_curated_and_sunset_are_disjoint() -> None:
    # The disjoint assert in constants.py fires at import time, but having
    # an explicit test guards the assert itself from being deleted.
    overlap = CURATED_PUBLIC_BUILTIN_AGENT_IDS & SUNSET_DEPRECATED_AGENT_IDS
    assert not overlap, f"Agents in both curated and sunset: {overlap}"
