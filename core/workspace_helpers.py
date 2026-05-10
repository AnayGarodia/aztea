"""Shared accessors for agents that want to consume `workspace_context`.

# OWNS: The single canonical way for any agent (built-in or third-party) to
#       extract a workspace bundle from a payload and render it for an LLM.
# NOT OWNS: Bundle construction (core/workspace_bundle.py), consent state
#           (core/workspace_consent.py).
# INVARIANTS:
#   - extract_workspace_context() never raises on malformed input; returns None.
#   - render_for_prompt() never returns more than `max_chars` characters.
"""

from __future__ import annotations

from typing import Any

from core.workspace_bundle import WorkspaceBundle, bundle_from_payload

WORKSPACE_CONTEXT_KEY = "workspace_context"
DEFAULT_PROMPT_BUDGET_CHARS = 3000


def extract_workspace_context(payload: Any) -> WorkspaceBundle | None:
    """Return the workspace bundle attached to `payload`, or None if absent.

    Tolerates payloads that are not dicts and bundle dicts that are missing
    optional fields. The contract: if you call this and get a `WorkspaceBundle`
    back, you may use any of its fields; if you get None, no context was sent
    or it was malformed.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get(WORKSPACE_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    if not raw.get("cwd_basename") and not raw.get("file_tree"):
        return None
    try:
        return bundle_from_payload(raw)
    except (ValueError, TypeError):
        return None


def render_for_prompt(
    bundle: WorkspaceBundle,
    max_chars: int = DEFAULT_PROMPT_BUDGET_CHARS,
) -> str:
    """Render a markdown block agents can paste into an LLM system prompt.

    Order: heading, branch, file tree, manifests, README. Sections are dropped
    from the bottom up if the output would exceed `max_chars`. The result is
    always within the budget; truncation is signalled inline.
    """
    sections = _render_sections(bundle)
    if max_chars <= 0:
        return ""
    return _join_within_budget(sections, max_chars)


def _render_sections(bundle: WorkspaceBundle) -> list[str]:
    sections: list[str] = []
    header = f"## Workspace context: `{bundle.cwd_basename}`"
    if bundle.git_branch:
        header += f" (branch: `{bundle.git_branch}`)"
    sections.append(header)
    if bundle.file_tree:
        sections.append("### File tree\n```\n" + bundle.file_tree + "\n```")
    if bundle.manifests:
        sections.append(_render_manifests(bundle.manifests))
    if bundle.readme_excerpt:
        sections.append(
            "### README excerpt\n```\n" + bundle.readme_excerpt + "\n```"
        )
    if bundle.truncated:
        sections.append(
            "_Note: workspace context was truncated to fit the size cap._"
        )
    return sections


def _render_manifests(manifests: dict[str, str]) -> str:
    parts = ["### Project manifests"]
    for name in sorted(manifests.keys()):
        body = manifests[name]
        parts.append(f"#### `{name}`\n```\n{body}\n```")
    return "\n".join(parts)


def strip_workspace_context(payload: Any) -> Any:
    """Return a shallow copy of `payload` with `workspace_context` removed.

    Privacy backstop: workspace bundles are per-call, MCP-attached context;
    they must never persist into work-example storage, audit hashes, or any
    other long-lived record. Callers in the work-example recording path call
    this immediately before persistence as the last-mile guard.

    Pure: never mutates `payload`. Returns the same object (no copy) when
    no work is required, so the no-op case is allocation-free.
    """
    if not isinstance(payload, dict):
        return payload
    if "workspace_context" not in payload:
        return payload
    cleaned = dict(payload)
    cleaned.pop("workspace_context", None)
    return cleaned


def _join_within_budget(sections: list[str], max_chars: int) -> str:
    out: list[str] = []
    used = 0
    for section in sections:
        addition = len(section) + (2 if out else 0)
        if used + addition > max_chars:
            remaining = max_chars - used
            if remaining > 64:
                out.append(section[: remaining - 16] + "\n... (truncated)")
            break
        out.append(section)
        used += addition
    return "\n\n".join(out)
