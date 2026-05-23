"""
author_style_reviewer.py — D17: review like a specific engineer would.

# v0 STATUS: requires the same hosted_index ingest as D16, plus a
#   per-author review-comment corpus. Without the corpus the agent
#   returns requires_configuration.
# REASONING LOOP: plan stylistic profile → synthesise review.
"""

from __future__ import annotations

from typing import Any

from agents._contracts import agent_error as _err
from agents._reasoning_scaffold import (
    clamp_int, requires_configuration, two_step_reasoning,
)
from core import hosted_index as _hi

_AGENT_SLUG = "author_style_reviewer"


def run(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    f"payload must be dict; got {type(payload).__name__}")
    repo_id = (payload.get("repo_id") or "").strip()
    author_handle = (payload.get("author_handle") or "").strip()
    hunks = payload.get("hunks")
    if not repo_id:
        return _err(f"{_AGENT_SLUG}.invalid_input", "repo_id is required")
    if not author_handle:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "author_handle is required")
    if not isinstance(hunks, list) or not hunks:
        return _err(f"{_AGENT_SLUG}.invalid_input",
                    "hunks must be a non-empty list")
    budget = clamp_int(payload.get("budget_cents"), 40, 1, 500)

    repo_row = _hi.store.get_repo(repo_id)
    if repo_row is None:
        return _err(f"{_AGENT_SLUG}.repo_not_indexed",
                    f"repo {repo_id!r} not in hosted index; ingest first")

    # The author-style corpus (the engineer's prior review comments) is
    # a separate ingest surface that v0 doesn't ship. Without it, the
    # agent has no signal to imitate, so it refuses honestly.
    return requires_configuration(
        _AGENT_SLUG,
        ["AZTEA_REVIEW_COMMENT_CORPUS_PATH"],
        "Author-Style Reviewer needs an ingested review-comment corpus "
        "scoped to the author. The repo is indexed but the corpus is "
        "not yet a hosted-index surface — coming in v0.1.",
        {"repo_id": repo_id, "author_handle": author_handle,
         "hunk_count": len(hunks)},
    )
