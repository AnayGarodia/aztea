# SPDX-License-Identifier: Apache-2.0
"""End-to-end backward compatibility for the Wave 2 MCP tool renames.

# OWNS: dispatch-time round-trip assertion that legacy tool names land on
#       the same handler as their Wave 2 canonical name. Complements the
#       static alias-map assertions in test_mcp_stdio_server.py by
#       exercising the actual call_tool path.
# INVARIANTS: every legacy tool name must reach the SAME handler invocation
#       as the canonical name with no observable behavior change.

Why this exists: aliases that exist in a map but aren't exercised in dispatch
are a common silent regression. A future refactor could remove the
``_LAZY_TOOL_NAME_ALIASES.get(...)`` call at the top of ``call_tool`` and the
alias-map test would still pass, while every cached Claude Code client would
break overnight. This test calls ``bridge.call_tool(legacy_name, ...)`` and
``bridge.call_tool(canonical_name, ...)`` and asserts the same internal
handler is invoked with the same arguments.
"""

from __future__ import annotations

import pytest

from aztea.mcp import server as _MODULE


# (legacy_name, canonical_name) — must mirror _LAZY_TOOL_NAME_ALIASES.
# Excludes manage_* (covered by meta_tools-specific tests) and call_agent
# (needs HTTP; covered by the catalog-stub e2e below).
_RENAME_PAIRS_NO_HTTP: list[tuple[str, str]] = [
    # Wave 2 legacy → Wave 2 canonical.
    ("search_specialists", "search_agents"),
    ("describe_specialist", "describe_agent"),
    # Pre-Wave-2 verb-style → Wave 2 canonical.
    ("aztea_search", "search_agents"),
    ("aztea_describe", "describe_agent"),
]


def _make_bridge(monkeypatch):
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(
        base_url="https://aztea.test", api_key="az_test",
    )
    bridge._auth_required = False
    bridge._entries = [
        {
            "agent_id": "agent-1",
            "tool_name": "python_code_executor",
            "tool": {
                "name": "python_code_executor",
                "description": "Execute Python snippets.",
                "input_schema": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                },
                "output_schema": {"type": "object"},
            },
            "catalog_metadata": {
                "category": "Code Execution",
                "slug": "python_code_executor",
                "price_per_call_usd": 0.0,
                "trust_score": 90,
            },
        }
    ]
    return bridge


@pytest.mark.parametrize("legacy,canonical", _RENAME_PAIRS_NO_HTTP)
def test_legacy_name_dispatches_to_same_handler_as_canonical(
    monkeypatch, legacy, canonical
):
    """A legacy name and its canonical name must hit the same internal
    handler with the same arguments. Spies on ``_search_catalog`` /
    ``_describe_catalog_entry`` so we observe the post-normalization call."""
    bridge_canonical = _make_bridge(monkeypatch)
    bridge_legacy = _make_bridge(monkeypatch)

    captured_canonical: dict = {}
    captured_legacy: dict = {}

    def _spy_search(self, query, **kwargs):
        target = (
            captured_canonical if self is bridge_canonical else captured_legacy
        )
        target["query"] = query
        target["kwargs"] = dict(kwargs)
        return {"results": [], "query": query, "off_catalog": True}

    def _spy_describe(self, slug):
        target = (
            captured_canonical if self is bridge_canonical else captured_legacy
        )
        target["slug"] = slug
        return {"slug": slug, "agent_id": "agent-1"}

    monkeypatch.setattr(
        _MODULE.RegistryBridge, "_search_catalog", _spy_search,
    )
    monkeypatch.setattr(
        _MODULE.RegistryBridge, "_describe_catalog_entry", _spy_describe,
    )

    if canonical == "search_agents":
        args = {"query": "lint this dockerfile"}
        bridge_canonical.call_tool(canonical, args)
        bridge_legacy.call_tool(legacy, args)
        assert captured_canonical and captured_canonical == captured_legacy, (
            f"legacy={legacy!r} did not reach the same handler as "
            f"canonical={canonical!r}: "
            f"canonical={captured_canonical!r} vs legacy={captured_legacy!r}"
        )
    elif canonical == "describe_agent":
        args = {"slug": "python_code_executor"}
        bridge_canonical.call_tool(canonical, args)
        bridge_legacy.call_tool(legacy, args)
        assert captured_canonical and captured_canonical == captured_legacy, (
            f"legacy={legacy!r} did not reach the same handler as "
            f"canonical={canonical!r}"
        )
    else:
        pytest.fail(f"unexpected canonical={canonical!r}")


def test_auto_call_agent_legacy_names_reach_auto_hire_endpoint(monkeypatch):
    """Both legacy names (`aztea_do`, `do_specialist_task`) and the new
    canonical name (`auto_call_agent`) must POST to /registry/agents/auto-hire.
    We capture the HTTP call to confirm dispatch normalization works for the
    do/auto-call branch (which has its own HTTP path, not the same as
    search/describe)."""
    captured_urls: list[str] = []

    class _FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"auto_invoked": False, "reason": "no_match"}

    class _FakeSession:
        def post(self, url, **_kwargs):
            captured_urls.append(url)
            return _FakeResponse()

    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)

    for name in ("auto_call_agent", "do_specialist_task", "aztea_do"):
        bridge = _MODULE.RegistryBridge(
            base_url="https://aztea.test", api_key="az_test",
        )
        bridge._auth_required = False
        bridge._session = _FakeSession()
        ok, payload = bridge.call_tool(name, {"intent": "audit deps"})
        assert ok is True, (
            f"name={name!r} dispatch failed: payload={payload!r}"
        )

    # Every entry point must have hit the same upstream URL exactly once.
    assert len(captured_urls) == 3
    assert all(
        url.endswith("/registry/agents/auto-hire") for url in captured_urls
    ), captured_urls


def test_call_agent_legacy_names_reject_self_as_slug(monkeypatch):
    """Round-trip the recursion guard from the legacy entry-point side: both
    `call_specialist` and `aztea_call` must refuse a lazy-tool slug just
    like `call_agent` does. Already covered structurally in
    test_mcp_stdio_server.py; this is the end-to-end version."""
    monkeypatch.setattr(_MODULE._feature_flags, "LAZY_MCP_SCHEMAS", True)
    bridge = _MODULE.RegistryBridge(
        base_url="https://aztea.test", api_key="az_test",
    )
    bridge._auth_required = False

    for entrypoint in ("call_agent", "call_specialist", "aztea_call"):
        ok, payload = bridge.call_tool(entrypoint, {"slug": "search_agents"})
        assert ok is False, f"entrypoint={entrypoint!r} should have rejected"
        assert payload["error"] == "INVALID_INPUT"
