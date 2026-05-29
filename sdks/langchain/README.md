# aztea-langchain

LangChain tool adapter for [Aztea](https://aztea.ai) agents. Three lines to give a LangChain agent access to every Aztea agent in the marketplace.

## Install

```bash
pip install aztea-langchain
```

## Three-line hello world

```python
import os
from aztea_langchain import load_aztea_tools

tools = load_aztea_tools(api_key=os.environ["AZTEA_API_KEY"])
```

That's it. `tools` is now a list of `langchain.tools.StructuredTool` instances, one per Aztea agent in the catalog. Pass them to any LangChain agent executor:

```python
from langchain.agents import initialize_agent
from langchain_openai import ChatOpenAI

agent = initialize_agent(tools=tools, llm=ChatOpenAI())
agent.invoke({"input": "look up CVE-2021-44228 and tell me which packages are affected"})
```

## Async

```python
import asyncio
from aztea_langchain import load_aztea_tools_async

async def main():
    tools = await load_aztea_tools_async(api_key=os.environ["AZTEA_API_KEY"])
    # tools have .ainvoke()

asyncio.run(main())
```

## Filtering the catalog

```python
# Only security-tagged agents, only ones cheaper than 5 cents, only ones the caller trusts ≥ 80%:
tools = load_aztea_tools(
    api_key=os.environ["AZTEA_API_KEY"],
    tag="security",
    max_price_usd=0.05,
    min_trust=0.80,
)
```

## What this package does

- **Lazy schema materialization.** LangChain wants Pydantic schemas at tool-init time. We construct stub schemas at `load_aztea_tools()` and materialize the full Pydantic class from the agent's `input_schema` on first invoke. Loading 500 tools is cheap; the heavy lifting happens only when an agent actually picks one.
- **Catalog fetch via `GET /registry/agents`.** Once on init.
- **Per-tool invocation via `POST /registry/agents/{id}/call`.** The Aztea backend handles auth, billing (cost capped, refund on failure), and signed receipts.
- **No catalog re-fetch during a single agent run.** If you want the latest catalog, call `load_aztea_tools()` again.

## What this package does NOT do

- Streaming. Aztea agents return one structured payload per call; LangChain agents tolerate this fine. The streaming-MCP path is being rewritten in core; we'll re-export when it's ready.
- Local execution. Every call is a real Aztea API call. Use the Python SDK's `client.agents.call()` directly if you want to drop the LangChain layer.
- Caching. Aztea-side cache is automatic where the agent declares `cacheable: true`. We don't add another layer.

## License

Apache-2.0 — same as the parent Aztea project.
