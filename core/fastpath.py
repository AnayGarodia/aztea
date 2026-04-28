"""Same-process fast-path helpers for local/internal agent execution."""

from __future__ import annotations

from typing import Any, Callable

from core import hosted_skills
from core import skill_executor


def run_local_agent(
    agent: dict[str, Any],
    payload: dict[str, Any],
    *,
    execute_builtin_agent: Callable[[str, dict[str, Any]], dict] | None = None,
    heartbeat_cb: Callable[[], None] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    endpoint_url = str(agent.get("endpoint_url") or "").strip()
    if hosted_skills.is_skill_endpoint(endpoint_url):
        skill_row = hosted_skills.get_hosted_skill_by_agent_id(str(agent.get("agent_id") or ""))
        if skill_row is None:
            raise RuntimeError("Hosted skill record is missing.")
        return True, skill_executor.execute_hosted_skill(skill_row, payload, heartbeat_cb=heartbeat_cb)

    if endpoint_url.startswith("internal://"):
        if execute_builtin_agent is None:
            raise RuntimeError(f"No built-in executor available for '{agent.get('agent_id')}'.")
        return True, execute_builtin_agent(str(agent.get("agent_id") or ""), payload)

    return False, None
