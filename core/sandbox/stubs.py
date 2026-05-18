"""Stub registry — now empty.

# OWNS: the structured-envelope template helpers that future deferred
#       verbs would use. Every spec-declared action now has a real
#       implementation; this module is kept as the template surface for
#       any new sandbox verbs added later.
"""

from __future__ import annotations

from typing import Any


def _browser_stub(description: str) -> dict[str, Any]:
    """Template for any future browser-pool verb that arrives stubbed.

    Why: kept so a new ``sandbox_browser_pdf`` (etc.) can adopt the same
    envelope shape callers already expect.
    """
    return {
        "planned_input_schema": {
            "type": "object",
            "required": ["sandbox_id", "session_id"],
            "properties": {
                "sandbox_id": {"type": "string"},
                "session_id": {"type": "string"},
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "js": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "planned_output_schema": {
            "type": "object",
            "properties": {
                "result": {"type": "object"},
                "screenshot_b64": {"type": "string"},
                "console_logs": {"type": "array"},
                "network": {"type": "array"},
            },
        },
        "tracking_issue": "live-sandbox: browser-pool follow-up verbs",
        "description": description,
        "reason": "Template only — no live stub verbs in this build.",
    }


def _simple_stub(
    *,
    issue: str,
    reason: str,
    in_props: dict[str, Any] | None = None,
    out_props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Template for non-browser stubs. Kept for future use."""
    return {
        "planned_input_schema": {
            "type": "object",
            "required": ["sandbox_id"],
            "properties": {
                "sandbox_id": {"type": "string"},
                **(in_props or {}),
            },
            "additionalProperties": False,
        },
        "planned_output_schema": {
            "type": "object",
            "properties": out_props or {},
        },
        "tracking_issue": issue,
        "reason": reason,
    }


# Every spec-declared verb is now a real handler in core.sandbox — see
# core/sandbox/__init__.py for the routing table. Adding a new stub:
# populate this dict using either _browser_stub or _simple_stub.
_STUB_TEMPLATES: dict[str, dict[str, Any]] = {}


def stub_for(action: str) -> dict[str, Any]:
    """Return the canonical stub envelope for ``action``.

    Why: the agent module still dispatches deferred actions through here
    so callers see a uniform shape regardless of which follow-up issue
    the action belongs to. With the registry now empty this only fires
    for verbs that exist in ALL_ACTIONS but lack a real handler — which
    today should never happen.
    """
    template = _STUB_TEMPLATES.get(action)
    if template is None:
        return {
            "stubbed": True,
            "action": action,
            "tracking_issue": "live-sandbox: unknown deferred action",
            "reason": "Action is reserved in the spec but not yet templated.",
        }
    return {
        "stubbed": True,
        "action": action,
        **template,
    }


def stub_actions() -> list[str]:
    """Pure: list every action verb backed by a stub envelope (now empty)."""
    return sorted(_STUB_TEMPLATES.keys())
