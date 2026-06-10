"""
skill_learnings.py — owner-facing routes for hosted-skill learnings review.

# OWNS: the two HTTP endpoints an owner uses to review and decide on proposed
#   learnings for their hosted skill.
#     GET  /skills/{skill_id}/learnings?status=proposed
#     POST /skills/{skill_id}/learnings/{learning_id}/decision  {accept|reject}
# NOT OWNS: the learnings store (core/skill_learnings.py), the distiller
#   (core/observability.py), or the execution-time injection.
# INVARIANTS:
#   - Gated by AZTEA_SELF_IMPROVEMENT: when off, both routes 404 (the surface
#     is hidden, matching the rest of the feature being inert).
#   - Owner-scoped: a caller may only see/decide learnings for a skill they own
#     (master bypasses, mirroring the other /skills routes). The learning must
#     also belong to the path skill_id.
# DECISIONS: factory pattern (create_router) so the shard-namespace auth helpers
#   are injected without an import cycle — same as server/routes/admin_usage.py.
#   core modules are imported directly (allowed: routes -> core).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from core import feature_flags as _feature_flags
from core import hosted_skills as _hosted_skills
from core import skill_learnings as _skill_learnings

logger = logging.getLogger(__name__)

# Map the buyer-facing decision verb onto the stored status.
_DECISION_TO_STATUS = {
    "accept": _skill_learnings.STATUS_ACTIVE,
    "reject": _skill_learnings.STATUS_ARCHIVED,
}
# Statuses an owner may filter the list by (None = all).
_LISTABLE_STATUSES = frozenset(
    {
        _skill_learnings.STATUS_PROPOSED,
        _skill_learnings.STATUS_ACTIVE,
        _skill_learnings.STATUS_ARCHIVED,
    }
)


def create_router(
    *,
    require_api_key: Callable[..., Any],
    require_scope: Callable[..., None],
) -> APIRouter:
    """Build the skill-learnings router with caller-supplied auth helpers."""
    router = APIRouter()

    def _gate_enabled() -> None:
        # 404 (not 403) when the feature is off so the surface stays invisible.
        if not _feature_flags.self_improvement_enabled():
            raise HTTPException(status_code=404, detail="Not found.")

    def _authorize_skill_owner(skill_id: str, caller: Any) -> dict:
        """Return the skill row after confirming the caller may manage it."""
        require_scope(caller, "worker")
        row = _hosted_skills.get_hosted_skill(skill_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Skill not found.")
        if caller["type"] != "master" and row.get("owner_id") != caller["owner_id"]:
            raise HTTPException(
                status_code=403, detail="Skill belongs to a different owner."
            )
        return row

    @router.get("/skills/{skill_id}/learnings")
    def list_skill_learnings(
        request: Request,
        skill_id: str,
        status: str | None = Query(default="proposed"),
        caller: Any = Depends(require_api_key),
    ) -> dict:
        _gate_enabled()
        _authorize_skill_owner(skill_id, caller)
        # Allow ?status=all (or empty) to return every status.
        normalized = None if status in (None, "", "all") else status
        if normalized is not None and normalized not in _LISTABLE_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown status '{status}'. "
                f"Expected one of {sorted(_LISTABLE_STATUSES)} or 'all'.",
            )
        learnings = _skill_learnings.list_learnings(skill_id, normalized)
        return {"skill_id": skill_id, "learnings": learnings}

    @router.post("/skills/{skill_id}/learnings/{learning_id}/decision")
    def decide_skill_learning(
        request: Request,
        skill_id: str,
        learning_id: str,
        body: dict = Body(default_factory=dict),
        caller: Any = Depends(require_api_key),
    ) -> dict:
        _gate_enabled()
        skill = _authorize_skill_owner(skill_id, caller)
        decision = str((body or {}).get("decision") or "").strip().lower()
        target_status = _DECISION_TO_STATUS.get(decision)
        if target_status is None:
            raise HTTPException(
                status_code=400,
                detail="decision must be 'accept' or 'reject'.",
            )
        learning = _skill_learnings.get_learning(learning_id)
        if learning is None or learning.get("skill_id") != skill_id:
            raise HTTPException(status_code=404, detail="Learning not found.")
        # Act as the skill's owner (so master can decide on any skill while the
        # store-layer rowcount guard still enforces owner+id integrity).
        effective_owner = str(skill.get("owner_id") or "")
        ok = _skill_learnings.set_learning_status(
            learning_id, effective_owner, target_status
        )
        if not ok:
            # Owner mismatch at the store layer or the row vanished mid-request.
            raise HTTPException(status_code=404, detail="Learning not found.")
        return {
            "learning_id": learning_id,
            "skill_id": skill_id,
            "status": target_status,
        }

    return router
