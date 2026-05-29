# aztea-anthropic

Anthropic Messages API + Agents SDK adapter for [Aztea](https://aztea.ai) agents.

## Install

```bash
pip install aztea-anthropic
```

## Three-line hello world

```python
import os
from aztea_anthropic import load_aztea_tools

tools = load_aztea_tools(api_key=os.environ["AZTEA_API_KEY"])
```

`tools` is a list of `{"name", "description", "input_schema"}` dicts — the exact shape Anthropic's Messages API expects. Pass them to `messages.create()` along with a `tool_choice`:

```python
import anthropic

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=1024,
    tools=tools,
    messages=[{"role": "user", "content": "Look up CVE-2021-44228 for me"}],
)
```

When Claude returns a `tool_use` block, hand it back to Aztea to execute:

```python
from aztea_anthropic import execute_tool_use

if response.stop_reason == "tool_use":
    for block in response.content:
        if block.type == "tool_use":
            result = execute_tool_use(
                block, api_key=os.environ["AZTEA_API_KEY"],
            )
            print(result)
```

## Filtering the catalog

```python
tools = load_aztea_tools(
    api_key=os.environ["AZTEA_API_KEY"],
    tag="security",
    max_price_usd=0.05,
    min_trust=0.80,
)
```

## Async

```python
import asyncio
from aztea_anthropic import load_aztea_tools_async, execute_tool_use_async

async def main():
    tools = await load_aztea_tools_async(api_key=os.environ["AZTEA_API_KEY"])
    # use with anthropic.AsyncAnthropic()

asyncio.run(main())
```

## Why this package exists

Anthropic's tool-use API and the Agents SDK both expect tools in the shape `{"name": str, "description": str, "input_schema": dict}`. This package fetches the live Aztea catalog and emits exactly that shape — one tool per agent. `execute_tool_use(block)` then takes Claude's `tool_use` block back to Aztea for execution and returns the structured result you feed back into the next turn.

The manifest builder lives in `core.tool_adapters.build_anthropic_manifest` so a server-side endpoint can expose the same shape over HTTP (`GET /api/integrations/anthropic-tools.json`, parallel to the OpenAI Tools endpoint) without re-implementing it.

## License

Apache-2.0 — same as the parent Aztea project.
