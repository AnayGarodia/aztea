# Aztea agent-harness integrations

Aztea installs into agent harnesses (not just editors) so an autonomous agent
defers "wedge" tasks — live web fetch/scrape, live CVE/DNS/package lookup,
sandboxed code execution — to an Aztea specialist instead of doing them itself.
Two halves per harness:

1. **MCP registration** — make Aztea's tools available. Shipped in the CLI:
   `aztea mcp install --client openclaw` / `--client hermes`.
2. **Deference** — make the agent actually *defer* on wedge tasks. A harness
   plugin/hook that runs the classifier and blocks the native tool, redirecting
   to `auto_call_agent`. Reference implementations live here.

On a no-human surface, deference is the product: nothing calls Aztea unless the
interceptor fires.

## The classification contract (source of truth)

`integrations/deference/classification-fixtures.json` is the language-neutral
spec for the deference classifier. Every implementation MUST satisfy it:

- **Python** — `sdks/python-sdk/aztea/cli/deference_core.py::classify_pretool_event`,
  guarded by `tests/test_deference_parity_fixture.py`.
- **OpenClaw (TS)** — `integrations/openclaw/aztea-deference-plugin.ts`, guarded
  by `aztea-deference-plugin.test.ts` (runs in the published plugin package's own
  CI, not this repo's pytest).

Both sides run the same fixture in their respective CIs, so the ported classifier
stays in lockstep and can't drift to under-detect wedge tasks (which would
silently cut Aztea call volume). Edit the fixture, never the assertions; keep both
sides green (TD1 parity guard). Add a case for any new regex branch.

Decision semantics: `block` = hard-redirect (block the native tool, surface the
reason), `warn` = advisory (surface the reason, don't block), `allow` = no-op.

The stable runtime contract a plugin reads is:
`aztea mcp pretool-hook --format json` → `{"decision":"block|warn|allow","reason":...}`
on stdout, exit 0. (The default `--format text` is the Claude Code hook protocol.)

## OpenClaw

Gate-verified against the OpenClaw source: `before_tool_call` is wired into the
agent loop (`agent-loop.ts` → `prepareToolCall`, honored before `tool.execute()`)
and can block. No fork or upstream PR needed.

1. Register the MCP server: `aztea mcp install --client openclaw`
   (writes `~/.openclaw/openclaw.json` under nested `mcp.servers`, tagged
   `AZTEA_CLIENT_ID=openclaw` so jobs are attributed).
2. Install the deference plugin (`aztea-deference-plugin.ts`) via OpenClaw's
   plugin system / ClawHub. It registers a `before_tool_call` hook that runs the
   in-process classifier (TD1: no per-tool-call subprocess) and returns
   `{ block, reason }`. Finalize the plugin manifest against your OpenClaw plugin
   SDK version; the hook name + result shape are stable.
3. The low-frequency prompt-scout path should shell out to
   `aztea mcp prompt-hook` so the network hardening stays single-sourced.

## Hermes

Gate-verified: Hermes has a `pre_tool_call` hook (`~/.hermes/config.yaml`
`hooks:` block) and a plugin pre-dispatch block path
(`agent/tool_executor.py` honors `get_pre_tool_call_block_message`).

1. Register the MCP server: `aztea mcp install --client hermes`
   (writes `~/.hermes/config.yaml` under top-level `mcp_servers`, YAML, tagged
   `AZTEA_CLIENT_ID=hermes`). Note: PyYAML required; comments in config.yaml are
   not preserved on write.
2. Deference via the `aztea-hermes-pretool.sh` adapter wired as a `pre_tool_call`
   hook. It translates the Hermes payload (`tool_name`/`args`, shell tool
   `terminal`) into the Aztea event shape and defers to
   `aztea mcp pretool-hook --format json`. Confirm the block-wiring convention
   against your Hermes version; for a hard block the plugin
   `get_pre_tool_call_block_message` path is the verified mechanism.

## Observability

`aztea mcp deference-log` shows recent decisions (the push side: what the hook
decided). Pair it with `jobs.client_id` (the settled side: what actually got
called and billed). `aztea mcp doctor --client <harness>` includes a
deterministic classifier self-test.
