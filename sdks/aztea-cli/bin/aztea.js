#!/usr/bin/env node
'use strict'

// 1.6.2: aztea-cli on npm is deprecated. The pip-installed `aztea` CLI now
// owns every surface the npm package used to handle, including the MCP
// stdio server (consolidated into `aztea.mcp.server`). Maintaining two
// implementations drifted them — the 1.6.1 co-pilot-mode P0 (broken steer)
// came directly from this JS server hardcoding the wrong request shape.
// See sdks/aztea-cli/README.md for migration notes.

const RED = '\x1b[31m'
const YELLOW = '\x1b[33m'
const CYAN = '\x1b[36m'
const BOLD = '\x1b[1m'
const RESET = '\x1b[0m'

process.stderr.write(`
${RED}${BOLD}aztea-cli on npm is deprecated.${RESET}

The full Aztea CLI is the Python package:

  ${CYAN}${BOLD}pip install aztea${RESET}
  ${CYAN}${BOLD}aztea login${RESET}
  ${CYAN}${BOLD}aztea init${RESET}       # registers Aztea as an MCP server in Claude Code / Cursor

The pip-installed ${BOLD}aztea${RESET} CLI ships the MCP server too — no Node required.

${YELLOW}If you came here from a tutorial that still says \`npm i -g aztea-cli\`,
that doc is out of date.${RESET}  The 1.6.1 co-pilot-mode breakage (silent
422 on ${BOLD}aztea_steer${RESET}) was the npm-shipped MCP server drifting from
the Python source. Consolidating to one implementation makes that whole
class of bug impossible.

The npm package will be removed in 30 days.
`)

process.exit(1)
