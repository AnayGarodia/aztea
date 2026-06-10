# Aztea agent-harness integrations

Aztea installs into agent harnesses (not just editors) so an autonomous agent
can hire an Aztea specialist for "wedge" tasks — live web fetch/scrape, live
CVE/DNS/package lookup, sandboxed code execution. Two halves per harness:

1. **MCP registration (the product surface)** — make Aztea's catalog available
   so the model can *elect* a specialist when it hits a capability wall.
   Shipped in the CLI: `aztea mcp install --client openclaw` / `--client hermes`.
2. **Deference observability** — a harness plugin/hook that runs the wedge
   classifier on every tool call and *logs* the moments a specialist could
   have been hired. Optional block modes redirect instead of logging; they are
   opt-in. Reference implementations live here.

**Pull, not push (2026-06-10).** The deference experiment
(`experiments/deference/REPORT.md`, 64 runs on real OpenClaw + Hermes installs)
showed that hard-blocking commodity tools is net-negative: equal-at-best
correctness, slower in 12 of 16 category-cells, ~2× token cost — while
specialists clearly won where the harness lacked the capability structurally
(e.g. dependency audits on Hermes, 4.7× faster than the agent improvising).
So the default mode everywhere is **warn = observe + log**; `block` /
`block-all` exist as opt-ins and experiment instruments. The deference log is
the demand signal — where agents hit wedge tasks — not a tollbooth.

## The classification contract (source of truth)

`integrations/deference/classification-fixtures.json` is the language-neutral
spec for the deference classifier. Every implementation MUST satisfy it:

- **Python** — `sdks/python-sdk/aztea/cli/deference_core.py::classify_pretool_event_for_mode`,
  guarded by `tests/test_deference_parity_fixture.py`.
- **OpenClaw (TS)** — `integrations/openclaw/aztea-deference/classifier.ts`,
  guarded by `index.test.ts` (runs in the published plugin package's own CI,
  not this repo's pytest).

Both sides run the same fixture in their respective CIs, so the ported classifier
stays in lockstep and can't drift to under-detect wedge tasks. Edit the fixture,
never the assertions; keep both sides green (TD1 parity guard). Add a case for
any new regex branch.

Decision semantics: `block` = hard-redirect (block the native tool, surface the
reason), `warn` = advisory/observe (log it; harnesses do not surface warns to
the model), `allow` = no-op. Modes: `warn` (default) | `block` (web tools only)
| `block-all` (every wedge category — experiment instrument).

The stable runtime contract a plugin reads is:
`aztea mcp pretool-hook --mode <mode> --format json` →
`{"decision":"block|warn|allow","reason":...}` on stdout, exit 0.
(The default `--format text` is the Claude Code hook protocol.)

## OpenClaw

Verified e2e on a real install (2026-06-10): plugin discovered + enabled,
hook blocks pre-execution, jobs land attributed.

1. Register the MCP server: `aztea mcp install --client openclaw`
   (writes `~/.openclaw/openclaw.json` under nested `mcp.servers`, tagged
   `AZTEA_CLIENT_ID=openclaw` so jobs are attributed).
2. Install the plugin package `integrations/openclaw/aztea-deference/`
   (manifest `openclaw.plugin.json` + `index.ts`; load via
   `plugins.load.paths` or ClawHub). It registers a `before_tool_call` hook,
   runs the in-process classifier (`classifier.ts` — no per-tool-call
   subprocess), normalizes OpenClaw's native tool names
   (`web_fetch`/`web_search`/`exec`/`browser`), and in block modes returns
   `{ block: true, blockReason }`. Mode comes from
   `plugins.entries["aztea-deference"].config.mode` (default `warn`).
3. The low-frequency prompt-scout path should shell out to
   `aztea mcp prompt-hook` so the network hardening stays single-sourced.

## Hermes

Verified e2e on a real install (2026-06-10): hook fires on `terminal`,
blocks in block modes, jobs land attributed.

1. Register the MCP server: `aztea mcp install --client hermes`
   (writes `~/.hermes/config.yaml` under top-level `mcp_servers`, YAML, tagged
   `AZTEA_CLIENT_ID=hermes`). Note: PyYAML required; comments in config.yaml are
   not preserved on write.
2. Wire `aztea-hermes-pretool.sh` as a `pre_tool_call` hook
   (`hooks: {pre_tool_call: [{command: …, timeout: 3}]}`). It translates the
   Hermes payload (`tool_input`; tool names `terminal`/`web_search`/
   `web_extract`) into the canonical event shape and defers to
   `aztea mcp pretool-hook --format json`. Mode via the
   `AZTEA_DEFERENCE_MODE` env var (default `warn`). Requires `jq`;
   fail-open on any error.

## Observability

`aztea mcp deference-log` shows recent decisions (the observed side: where
wedge tasks happened and what the hook decided). Pair it with
`jobs.client_id` (the settled side: what actually got hired and billed).
`aztea mcp doctor --client <harness>` includes a deterministic classifier
self-test.
