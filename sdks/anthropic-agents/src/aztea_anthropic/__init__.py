"""aztea-anthropic — Anthropic Messages API + Agents SDK adapter for Aztea.

Three lines to hand Anthropic's Messages API every Aztea agent as a tool:

    from aztea_anthropic import load_aztea_tools
    tools = load_aztea_tools(api_key=os.environ["AZTEA_API_KEY"])

`tools` is a list of `{"name", "description", "input_schema"}` dicts — the
exact shape Anthropic's tool-use API expects. When Claude returns a
`tool_use` block, hand it to `execute_tool_use(block)` to run on Aztea
and get the structured result back for the next turn.
"""

from __future__ import annotations

from ._factory import (
    execute_tool_use,
    execute_tool_use_async,
    load_aztea_tools,
    load_aztea_tools_async,
)

__all__ = [
    "load_aztea_tools",
    "load_aztea_tools_async",
    "execute_tool_use",
    "execute_tool_use_async",
]
__version__ = "0.1.0"
