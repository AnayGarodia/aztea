#!/usr/bin/env node
'use strict'

// aztea-cli is a thin npm shim. It owns two responsibilities:
//   1. `init`  — one-command setup that registers Aztea as an MCP server
//                in Claude Code (so users in the JS ecosystem don't need
//                Python on the path to get started).
//   2. `mcp`   — runs the stdio MCP server process. Editors spawn this.
// Everything else (hire, jobs, wallet, agents, mcp install/doctor/uninstall)
// lives in the Python `aztea` CLI. We point users there.

const [,, cmd, ...rest] = process.argv

const PY_HINT = `
The full Aztea CLI (hire, jobs, wallet, agents, pipelines, …) is the
Python package:

  pip install aztea
  aztea login
  aztea --help
`

switch (cmd) {
  case 'init':
    require('../src/init.js').run(rest)
    break
  case 'login': {
    // aztea login --api-key az_xxx  — non-interactive setup for Claude Code.
    const keyFlag = rest.find(a => a.startsWith('--api-key=') || a === '--api-key')
    let apiKey = ''
    if (keyFlag && keyFlag.includes('=')) {
      apiKey = keyFlag.split('=').slice(1).join('=').trim()
    } else if (keyFlag) {
      const keyIdx = rest.indexOf('--api-key')
      apiKey = (rest[keyIdx + 1] || '').trim()
    }
    if (!apiKey) {
      console.error('Usage: aztea login --api-key az_...')
      process.exit(1)
    }
    require('../src/init.js').loginWithKey(apiKey)
    break
  }
  case 'mcp':
    require('../src/mcp-server.js').run()
    break
  case 'whoami':
    require('../src/init.js').whoami()
    break
  case 'hire':
  case 'jobs':
  case 'wallet':
  case 'agents':
  case 'pipelines':
  case 'logout':
    console.log(PY_HINT.trim())
    process.exit(0)
    break
  default:
    console.log(`Aztea CLI (npm)

This package is a thin shim. It exists to:
  1. Set up Aztea as an MCP server in Claude Code.
  2. Run the stdio MCP server when an editor spawns it.

Usage:
  npx -y aztea-cli@latest init                     Set up Aztea in Claude Code (creates account)
  npx -y aztea-cli@latest login --api-key az_...   Configure with an existing API key
  npx -y aztea-cli@latest whoami                   Show the current account
  npx -y aztea-cli@latest mcp                      Start the MCP server (called by editors)
${PY_HINT}`)
    if (cmd && cmd !== '--help' && cmd !== '-h') {
      console.error(`Unknown command: ${cmd}`)
      process.exit(1)
    }
}
