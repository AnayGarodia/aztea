# aztea-cli (npm) — deprecated

> **This npm package is deprecated as of 1.6.2.** Install the Aztea CLI
> via pip instead. The pip-installed `aztea` CLI now ships the MCP server
> directly — no Node required.

## Why

Pre-1.6.2 there were **two** MCP server implementations in this repo:

- the Python server at `scripts/aztea_mcp_server.py` (the source of truth
  in dev), and
- a JavaScript port at `sdks/aztea-cli/src/mcp-server.js` (the npm-shipped
  binary that users actually ran via `aztea mcp install`).

They drifted. The JS server posted the legacy request shape
`{msg_type: "steer", payload: {…}}` to `/jobs/{id}/messages`, while the
Python server posted `{"message": "…"}` to the dedicated
`/jobs/{id}/steer` endpoint. That drift was the 1.6.1 power-user eval's
headline P0: co-pilot-mode steer 422'd in every Claude Code session
running the npm CLI, even though the Python source was correct.

1.6.2 consolidates to one implementation, in Python, shipped via pip.

## Migration

```sh
# 1. Remove the deprecated npm package.
npm uninstall -g aztea-cli

# 2. Install the consolidated CLI.
pip install aztea
aztea login
aztea init       # registers Aztea as an MCP server in Claude Code / Cursor
                 # and appends a marketplace-correct CLAUDE.md snippet
```

Editor MCP configs created by the old `aztea mcp install` still work
unchanged — they were already pointing at `command: aztea` (resolved via
PATH); the difference is that `aztea` is now the pip entrypoint and
`aztea mcp serve` runs the in-process Python server.

## Timeline

- **1.6.2** — this package starts emitting the deprecation notice and
  exiting non-zero on every invocation.
- **30 days after 1.6.2 release** — the package is unpublished from npm.

If you depend on Node-only tooling that needs an MCP-server binary, the
right move is to call `aztea mcp serve` from your tooling — it speaks the
same stdio JSON-RPC over the same protocol.
