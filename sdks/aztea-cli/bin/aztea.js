#!/usr/bin/env node
'use strict'

const [,, cmd, ...rest] = process.argv

switch (cmd) {
  case 'init':
    require('../src/init.js').run(rest)
    break
  case 'mcp':
    require('../src/mcp-server.js').run()
    break
  default:
    console.log(`Aztea CLI

Usage:
  npx aztea-cli init     Add Aztea to Claude Code (creates account + writes MCP config)
  npx aztea-cli mcp      Start the MCP server (called by Claude Code automatically)
`)
    if (cmd && cmd !== '--help' && cmd !== '-h') {
      console.error(`Unknown command: ${cmd}`)
      process.exit(1)
    }
}
