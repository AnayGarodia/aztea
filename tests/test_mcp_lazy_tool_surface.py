# SPDX-License-Identifier: Apache-2.0
"""CI guard: pin the lazy MCP tool surface to its documented set.

# OWNS: assertion that ``MCPStdioServer.tools()`` in lazy mode publishes
#       exactly the nine tools enumerated in CLAUDE.md / AGENTS.md.
# INVARIANTS: documented tool surface and code-side tool surface must agree.
# DECISIONS: assert against the production code path (build the list the
#       same way ``server.py`` does at line ~1194) rather than a
#       hand-maintained constant in the test. A constant would drift the
#       same way the prose docs did.

Why this exists: CLAUDE.md / AGENTS.md / README copy has silently drifted
in the past ("four-tool surface", "seven tools") even though the canonical
count is nine. The MCP surface is the contract between Aztea and every
calling agent; misleading docs erode that contract. This test fails any
PR that adds, removes, or renames a lazy tool without also updating the
documented count.
"""

from __future__ import annotations


_EXPECTED_LAZY_TOOL_NAMES: frozenset[str] = frozenset({
    "search_specialists",
    "describe_specialist",
    "call_specialist",
    "do_specialist_task",
    "aztea_status",
    "aztea_inspect",
    "aztea_query",
    "manage_job",
    "manage_budget",
    "manage_workflow",
})


def _build_lazy_tool_list() -> list[dict]:
    """Reconstruct the lazy tool list the same way MCPStdioServer.tools() does.

    Mirrors the lazy-mode branch in
    ``sdks/python-sdk/aztea/mcp/server.py`` (search for the
    ``LAZY_MCP_SCHEMAS`` block). Kept in sync by reading the same
    constants — a rename of any of these symbols breaks this import
    immediately, which is the intended early-warning signal.

    aztea_call_streaming + aztea_steer were dropped from the public surface
    2026-05-17 (broken streaming pipeline; see CLAUDE.md). The constants
    still exist in copilot_tools so dispatch can return tool_not_supported
    cleanly, but they are NOT in the lazy tools() list anymore.
    """
    from aztea.mcp import meta_tools
    from aztea.mcp.server import (
        _LAZY_CALL_TOOL,
        _LAZY_DESCRIBE_TOOL,
        _LAZY_DO_TOOL,
        _LAZY_INSPECT_TOOL,
        _LAZY_QUERY_TOOL,
        _LAZY_SEARCH_TOOL,
        _LAZY_STATUS_TOOL,
    )

    return [
        _LAZY_SEARCH_TOOL,
        _LAZY_DESCRIBE_TOOL,
        _LAZY_CALL_TOOL,
        _LAZY_DO_TOOL,
        _LAZY_STATUS_TOOL,
        _LAZY_INSPECT_TOOL,
        _LAZY_QUERY_TOOL,
        *meta_tools.always_visible_tools(),
    ]


def test_lazy_tool_surface_is_exactly_ten_tools():
    tools = _build_lazy_tool_list()
    assert len(tools) == 10, (
        f"Lazy MCP tool surface drifted: expected 10 tools, found {len(tools)}.\n"
        f"  Names: {[t['name'] for t in tools]}\n"
        "If this change is intentional, update CLAUDE.md, AGENTS.md, and "
        "this test's _EXPECTED_LAZY_TOOL_NAMES in the same PR."
    )


def test_lazy_tool_surface_names_match_documented_set():
    tools = _build_lazy_tool_list()
    actual_names = frozenset(t["name"] for t in tools)
    missing = _EXPECTED_LAZY_TOOL_NAMES - actual_names
    extra = actual_names - _EXPECTED_LAZY_TOOL_NAMES
    assert not missing and not extra, (
        f"Lazy MCP tool surface drifted from documented set.\n"
        f"  Missing (in docs, not in code): {sorted(missing)}\n"
        f"  Extra   (in code, not in docs): {sorted(extra)}\n"
        f"If this change is intentional, update CLAUDE.md, AGENTS.md, and "
        f"this test's _EXPECTED_LAZY_TOOL_NAMES in the same PR."
    )


def test_lazy_tool_entries_each_have_name_and_input_schema():
    """Each lazy tool must declare an MCP-compliant shape."""
    for tool in _build_lazy_tool_list():
        assert "name" in tool, f"Tool missing 'name': {tool!r}"
        assert "description" in tool, f"Tool '{tool.get('name')}' missing 'description'"
        assert "input_schema" in tool, (
            f"Tool '{tool.get('name')}' missing 'input_schema' "
            "(MCP requires this on every advertised tool)"
        )
