"""
judge.py — "did this past commit cause a later bug?" signal generator.

# OWNS: did_this_change_cause_a_bug → BugSignal with severity + citations.
# NOT OWNS: retrieval (retrieve.py), embeddings (embed.py), DB rows (store.py).
#
# INVARIANTS:
#   * Returns a BugSignal in every code path — never raises for "unknown".
#   * Citations are commit SHAs or incident IDs the caller can resolve.
#   * Severity is monotonic: stronger evidence yields stronger severity.
#
# DECISIONS:
#   * Signals are graded, not boolean. A single revert is "moderate"; a
#     revert plus a customer-labelled incident is "strong"; nothing at all
#     is "none". This lets the reviewer agent weight the evidence rather
#     than treating every revert as a smoking gun.
#   * Heuristics first, LLM second. The relational signal (was_reverted,
#     hotfix_for, incidents_referencing) is deterministic and free. The
#     LLM lift (look at the diff content + the alleged fix to argue
#     causality) is left for a follow-up.
"""

from __future__ import annotations

import logging

from core.hosted_index import store as _store
from core.hosted_index.types import BugSignal

_LOG = logging.getLogger(__name__)


def did_this_change_cause_a_bug(commit_sha: str, repo_id: str) -> BugSignal:
    """Generate a BugSignal for the named commit.

    Severity ladder:
      * strong   — both an incident referencing this commit AND a hotfix.
      * moderate — either: a hotfix_for entry whose target is this commit,
                   OR was_reverted == True (someone reverted this commit),
                   OR an incident references this commit.
      * weak     — placeholder for future LLM-derived signal (TODO).
      * none     — no relational evidence and no incident link.

    Why not boolean: caller (D16) wants to weight the warning. "Strong"
    might justify a blocker-style PR comment; "moderate" justifies a note;
    "none" should produce no comment at all.
    """
    if not isinstance(commit_sha, str) or not commit_sha.strip():
        raise ValueError("commit_sha must be a non-empty string")
    if not isinstance(repo_id, str) or not repo_id.strip():
        raise ValueError("repo_id must be a non-empty string")

    commit = _store.get_commit(repo_id, commit_sha)
    if commit is None:
        return BugSignal(
            severity="none",
            reasons=("commit not in index — cannot judge",),
        )

    citations: list[str] = []
    reasons: list[str] = []

    # Signal 1: was the commit itself reverted?
    if int(commit.get("was_reverted") or 0):
        citations.append(commit_sha)
        reasons.append("commit was reverted")

    # Signal 2: did a later commit declare itself a hotfix for this one?
    hotfixes = _store.list_hotfix_commits_for(repo_id, commit_sha)
    for hf in hotfixes:
        citations.append(hf["commit_sha"])
    if hotfixes:
        reasons.append(f"{len(hotfixes)} hotfix commit(s) reference this commit")

    # Signal 3: any incident references this commit?
    incidents = _store.incidents_referencing(repo_id, commit_sha)
    for inc in incidents:
        citations.append(inc["incident_id"])
    if incidents:
        reasons.append(f"{len(incidents)} incident(s) reference this commit")

    severity = _severity_from_signals(
        was_reverted=bool(int(commit.get("was_reverted") or 0)),
        hotfix_count=len(hotfixes),
        incident_count=len(incidents),
    )
    return BugSignal(
        severity=severity,
        citations=tuple(citations),
        reasons=tuple(reasons),
    )


def _severity_from_signals(
    *, was_reverted: bool, hotfix_count: int, incident_count: int,
) -> str:
    """Pure: severity ladder applied to the three relational signals.

    Why pure: the rule needs to be easy to inspect and easy to unit-test.
    Mixing it inline with the DB lookups makes both harder.
    """
    if incident_count > 0 and (was_reverted or hotfix_count > 0):
        return "strong"
    if incident_count > 0 or was_reverted or hotfix_count > 0:
        return "moderate"
    return "none"
