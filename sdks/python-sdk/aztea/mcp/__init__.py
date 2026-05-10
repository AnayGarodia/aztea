"""Aztea MCP server — single source of truth.

Exposes the Aztea registry as an MCP stdio server so coding-agent hosts
(Claude Code, Cursor, Codex, Gemini, …) can hire specialists by tool name.

Entry points:
    aztea-mcp                    console_script (see pyproject.toml)
    aztea mcp serve              CLI subcommand (see aztea.cli.mcp:serve)
    python -m aztea.mcp.server   direct invocation

Before 1.6.2 this lived in three places:
    scripts/aztea_mcp_server.py        (the Python server)
    scripts/aztea_mcp_meta_tools.py    (platform tools)
    scripts/aztea_mcp_copilot_tools.py (streaming + steer)
    sdks/aztea-cli/src/mcp-server.js   (a *separate* JS implementation,
                                        drifted; shipped on npm as
                                        aztea-cli; deprecated and deleted
                                        in 1.6.2)

The JS server posted ``{msg_type: "steer", payload: {...}}`` to
``/jobs/{id}/messages`` while the Python server posted ``{"message": ...}``
to ``/jobs/{id}/steer``. The drift meant co-pilot mode worked when invoked
from the dev tree but 422'd / 500'd through the npm CLI. Consolidating to
one Python implementation makes that whole class of bug impossible by
construction.
"""

from .server import main as main  # re-export for `python -m aztea.mcp`

__all__ = ["main"]
