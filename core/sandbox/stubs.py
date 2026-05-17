"""Structured stub envelopes for the v0-deferred surface.

# OWNS: every action listed in the spec that v0 does NOT implement returns a
#       full envelope with ``planned_input_schema`` + ``planned_output_schema``
#       + ``tracking_issue`` — never a bare {"error": "unsupported"}.
# INVARIANTS:
#   * Each entry's planned_input_schema and planned_output_schema must be
#     valid JSON Schema (the test suite parses every entry and validates).
#   * Each entry has a tracking_issue title so the follow-up work is
#     enumerable from the codebase alone.
"""

from __future__ import annotations

from typing import Any


def _browser_stub(description: str) -> dict[str, Any]:
    """Return a generic browser-action stub envelope.

    Why: every browser verb shares the same shape; centralising the
    template means the dozen browser stubs stay in sync.
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
        "tracking_issue": "live-sandbox: Playwright/CDP browser session pool",
        "description": description,
        "reason": (
            "Browser surface lands as a single follow-up issue covering the "
            "Playwright pool, per-session eviction, cookie isolation, and "
            "PDF/screenshot artefact storage."
        ),
    }


def _simple_stub(
    *,
    issue: str,
    reason: str,
    in_props: dict[str, Any] | None = None,
    out_props: dict[str, Any] | None = None,
) -> dict[str, Any]:
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


_STUB_TEMPLATES: dict[str, dict[str, Any]] = {}

_STUB_TEMPLATES["sandbox_browser_session"] = {
    "planned_input_schema": {
        "type": "object",
        "required": ["sandbox_id"],
        "properties": {
            "sandbox_id": {"type": "string"},
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                },
            },
            "headless": {"type": "boolean", "default": True},
        },
        "additionalProperties": False,
    },
    "planned_output_schema": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "cdp_url": {"type": "string"},
        },
    },
    "tracking_issue": "live-sandbox: Playwright/CDP browser session pool",
    "reason": (
        "Managed Playwright pool with per-session eviction. Decoupled from "
        "the engine PR so browser infra can ship on its own cadence."
    ),
}

for verb, desc in {
    "sandbox_browser_navigate": "Navigate the session to URL",
    "sandbox_browser_click": "Click selector",
    "sandbox_browser_fill": "Fill selector with value",
    "sandbox_browser_screenshot": "Take a full-page screenshot",
    "sandbox_browser_console_logs": "Read console logs",
    "sandbox_browser_network": "Read network captures",
    "sandbox_browser_a11y_tree": "Return the a11y tree",
    "sandbox_browser_eval": "Evaluate JS in the page",
    "sandbox_browser_axe_audit": "Run axe-core accessibility audit",
    "sandbox_browser_lighthouse": "Run Lighthouse audit",
    "sandbox_browser_record": "Record a click sequence",
    "sandbox_browser_replay": "Replay a recorded sequence",
}.items():
    _STUB_TEMPLATES[verb] = _browser_stub(desc)

_STUB_TEMPLATES["sandbox_tunnel_open"] = {
    "planned_input_schema": {
        "type": "object",
        "required": ["sandbox_id", "service", "port"],
        "properties": {
            "sandbox_id": {"type": "string"},
            "service": {"type": "string"},
            "port": {"type": "integer"},
            "auth": {"type": "string", "enum": ["bearer", "none"]},
            "hostname_hint": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "planned_output_schema": {
        "type": "object",
        "properties": {
            "tunnel_id": {"type": "string"},
            "public_url": {"type": "string"},
            "expires_at": {"type": "integer"},
        },
    },
    "tracking_issue": "live-sandbox: public tunnels with TLS + edge auth",
    "reason": (
        "Public tunnels require an edge proxy (Caddy/Cloudflare) provisioned "
        "alongside the engine. Out of scope for the engine PR."
    ),
}
_STUB_TEMPLATES["sandbox_tunnel_close"] = _STUB_TEMPLATES["sandbox_tunnel_open"]

_STUB_TEMPLATES["sandbox_webhook_inbox"] = _simple_stub(
    issue="live-sandbox: webhook inbox + replay",
    reason=(
        "Webhook capture requires a per-sandbox proxy in front of the tunnel "
        "to record incoming Stripe/GitHub payloads. Builds on the tunnel "
        "issue above."
    ),
    out_props={"events": {"type": "array"}, "count": {"type": "integer"}},
)
_STUB_TEMPLATES["sandbox_outbound_record"] = _simple_stub(
    issue="live-sandbox: outbound HTTP record/replay (VCR layer)",
    reason=(
        "Deterministic record/replay of outbound HTTP belongs as its own "
        "layer next to the upcoming Aztea outbound recorder."
    ),
    in_props={"mode": {"type": "string", "enum": ["record", "replay", "off"]}},
    out_props={"cassettes": {"type": "array"}},
)
_STUB_TEMPLATES["sandbox_outbound_replay"] = _STUB_TEMPLATES["sandbox_outbound_record"]
_STUB_TEMPLATES["sandbox_inject_failure"] = _simple_stub(
    issue="live-sandbox: chaos/failure injection",
    reason=(
        "Packet loss + latency injection requires NET_ADMIN-capable sidecars; "
        "tracked separately so the v0 default-deny posture stays intact."
    ),
    in_props={
        "target": {"type": "string"},
        "kind": {"type": "string", "enum": ["latency", "loss", "abort"]},
        "value": {"type": "number"},
    },
)
_STUB_TEMPLATES["sandbox_network_capture"] = _simple_stub(
    issue="live-sandbox: tcpdump + PCAP export",
    reason="Requires NET_RAW-capable sidecar; tracked alongside chaos injection.",
)
_STUB_TEMPLATES["sandbox_trace"] = _simple_stub(
    issue="live-sandbox: strace/dtrace/py-spy attach",
    reason=(
        "Process attach requires PTRACE_ATTACH and varies per host kernel; "
        "deferred to a privileged-helper PR."
    ),
    in_props={
        "pid": {"type": "integer"},
        "tool": {"type": "string", "enum": ["strace", "py-spy", "perf"]},
    },
)
_STUB_TEMPLATES["sandbox_link"] = _simple_stub(
    issue="live-sandbox: multi-sandbox network linking",
    reason=(
        "Cross-sandbox docker network attach; tracked as a follow-up so the "
        "v0 single-sandbox path doesn't depend on it."
    ),
    in_props={"other_sandbox_id": {"type": "string"}},
)
_STUB_TEMPLATES["sandbox_batch_start"] = _simple_stub(
    issue="live-sandbox: matrix batch start",
    reason=(
        "N-way matrix boots compose with the wallet hold layer; tracked "
        "separately to avoid blocking the engine PR on wallet integration."
    ),
    in_props={"matrix": {"type": "object"}},
    out_props={"sandbox_ids": {"type": "array"}},
)
_STUB_TEMPLATES["sandbox_share"] = _simple_stub(
    issue="live-sandbox: shared read-only / collab sessions",
    reason="Edge multiplexer required for terminal-share; v0 stays single-actor.",
    in_props={"access": {"type": "string", "enum": ["read", "full"]}},
    out_props={"share_url": {"type": "string"}},
)
_STUB_TEMPLATES["sandbox_export_snapshot"] = _simple_stub(
    issue="live-sandbox: export snapshot to user-owned bucket",
    reason="Snapshot export needs Aztea wallet bucket creds — tracked next to the wallet PR.",
    in_props={"destination_uri": {"type": "string"}},
)


def stub_for(action: str) -> dict[str, Any]:
    """Return the canonical stub envelope for ``action``.

    Why: the agent module dispatches deferred actions here so callers see
    a uniform shape regardless of which follow-up issue the action
    belongs to.
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
    """Pure: list every action verb backed by a stub envelope."""
    return sorted(_STUB_TEMPLATES.keys())
