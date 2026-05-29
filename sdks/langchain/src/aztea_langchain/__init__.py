"""aztea-langchain — LangChain tool adapter for Aztea agents.

Three lines to give a LangChain agent access to the Aztea marketplace:

    from aztea_langchain import load_aztea_tools
    tools = load_aztea_tools(api_key=os.environ["AZTEA_API_KEY"])

Tools are `langchain.tools.StructuredTool` instances; pass them to any
LangChain agent / chain / executor. Each tool's `.invoke(input)` calls
`POST /registry/agents/{id}/call` on the Aztea backend, which handles
auth, billing (cost capped per call, automatic refund on failure), and
signed Ed25519 receipts.

See `_factory.load_aztea_tools` for the full kwargs surface (category /
max_price_usd / min_trust filters, base_url override, timeout).
"""

from __future__ import annotations

from ._factory import (
    load_aztea_tools,
    load_aztea_tools_async,
)

__all__ = ["load_aztea_tools", "load_aztea_tools_async"]
__version__ = "0.1.0"
