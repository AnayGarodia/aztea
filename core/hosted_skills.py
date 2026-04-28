"""
hosted_skills.py — Storage layer for OpenClaw SKILL.md files Aztea executes
on behalf of skill builders.

A hosted skill is paired 1:1 with an entry in the ``agents`` table whose
``endpoint_url`` is ``skill://{skill_id}``. The agent row carries everything
the registry, jobs, settlement, ratings, dispute, MCP, and identity layers
need. This module stores only the SKILL.md-specific bits: the raw text, the
parsed metadata, and the system prompt the executor uses at run time.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from core import db as _db
from core.registry.core_schema import _resolved_db_path


SKILL_ENDPOINT_SCHEME = "skill://"

_DEFAULT_TEMPERATURE = 0.2
_DEFAULT_MAX_OUTPUT_TOKENS = 1500
_MAX_OUTPUT_TOKENS_HARD_CAP = 4000


def _conn() -> sqlite3.Connection:
    return _db.get_raw_connection(_resolved_db_path())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_skill_endpoint_url(skill_id: str) -> str:
    return f"{SKILL_ENDPOINT_SCHEME}{skill_id}"


def parse_skill_id_from_endpoint(endpoint_url: str) -> str | None:
    value = (endpoint_url or "").strip()
    if not value.startswith(SKILL_ENDPOINT_SCHEME):
        return None
    sid = value[len(SKILL_ENDPOINT_SCHEME):].strip().rstrip("/")
    return sid or None


def is_skill_endpoint(endpoint_url: str | None) -> bool:
    return parse_skill_id_from_endpoint(endpoint_url or "") is not None


def create_hosted_skill(
    *,
    agent_id: str,
    owner_id: str,
    slug: str,
    raw_md: str,
    system_prompt: str,
    parsed_metadata: dict[str, Any] | None = None,
    model_chain: list[str] | None = None,
    temperature: float = _DEFAULT_TEMPERATURE,
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
) -> dict[str, Any]:
    """Persist a hosted SKILL.md and return the created skill row.

    The backing agent must already be registered before calling this.
    ``raw_md`` is the original SKILL.md text; ``system_prompt`` is the
    extracted prompt that will be used at inference time.
    """
    if not agent_id:
        raise ValueError("agent_id is required.")
    if not owner_id:
        raise ValueError("owner_id is required.")
    if not slug:
        raise ValueError("slug is required.")
    if not raw_md.strip():
        raise ValueError("raw_md is required.")
    if not system_prompt.strip():
        raise ValueError("system_prompt is required.")

    if temperature < 0 or temperature > 2:
        raise ValueError("temperature must be in [0, 2].")
    capped_tokens = max(1, min(int(max_output_tokens), _MAX_OUTPUT_TOKENS_HARD_CAP))

    skill_id = str(uuid.uuid4())
    now = _now_iso()
    metadata_json = json.dumps(parsed_metadata or {}, sort_keys=True, default=str)
    chain_json = json.dumps(list(model_chain)) if model_chain else None

    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO hosted_skills
                (skill_id, agent_id, owner_id, slug, raw_md, system_prompt,
                 parsed_metadata_json, model_chain, temperature, max_output_tokens,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                skill_id,
                agent_id,
                owner_id,
                slug,
                raw_md,
                system_prompt,
                metadata_json,
                chain_json,
                float(temperature),
                capped_tokens,
                now,
                now,
            ),
        )
    return get_hosted_skill(skill_id) or {}


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    metadata_text = out.get("parsed_metadata_json") or "{}"
    try:
        out["parsed_metadata"] = json.loads(metadata_text) if isinstance(metadata_text, str) else {}
    except json.JSONDecodeError:
        out["parsed_metadata"] = {}
    chain_text = out.get("model_chain")
    try:
        out["model_chain"] = json.loads(chain_text) if chain_text else None
    except (TypeError, json.JSONDecodeError):
        out["model_chain"] = None
    return out


def get_hosted_skill(skill_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM hosted_skills WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()
    return _row_to_dict(row)


def get_hosted_skill_by_agent_id(agent_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM hosted_skills WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
    return _row_to_dict(row)


def list_hosted_skills_for_owner(owner_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return all hosted skills owned by ``owner_id``, newest first, capped at 500."""
    capped = max(1, min(int(limit), 500))
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM hosted_skills
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, capped),
        ).fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


def delete_hosted_skill(skill_id: str) -> bool:
    with _conn() as conn:
        result = conn.execute(
            "DELETE FROM hosted_skills WHERE skill_id = ?",
            (skill_id,),
        )
    return result.rowcount > 0


def list_pending_skill_agent_ids() -> list[str]:
    """Every agent ID that has a hosted skill row.

    Used by the async worker loop to extend the set of agents it scans.
    Returns an empty list when the ``hosted_skills`` table doesn't exist
    yet — the worker thread is allowed to start before migrations have
    finished applying on a fresh database.
    """
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT agent_id FROM hosted_skills"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r["agent_id"]) for r in rows]
