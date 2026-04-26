'use strict'

const https = require('https')
const http = require('http')
const fs = require('fs')
const path = require('path')
const os = require('os')
const readline = require('readline')
const { execFileSync, spawnSync } = require('child_process')

const BASE_URL = process.env.AZTEA_BASE_URL || 'https://aztea.ai'

// ── HTTP helpers ─────────────────────────────────────────────

const REQUEST_TIMEOUT_MS = 30_000

function post(url, body) {
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body)
    const parsed = new URL(url)
    const lib = parsed.protocol === 'https:' ? https : http
    let timer
    const req = lib.request({
      hostname: parsed.hostname,
      port: parsed.port || (parsed.protocol === 'https:' ? 443 : 80),
      path: parsed.pathname + (parsed.search || ''),
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
        'User-Agent': 'aztea-cli/0.5.0',
      },
    }, (res) => {
      let data = ''
      res.on('data', d => { data += d })
      res.on('end', () => {
        clearTimeout(timer)
        try {
          resolve({ status: res.statusCode, body: JSON.parse(data) })
        } catch {
          resolve({ status: res.statusCode, body: data })
        }
      })
    })
    req.on('error', err => {
      clearTimeout(timer)
      reject(err)
    })
    timer = setTimeout(() => {
      req.destroy(new Error(`Request timed out after ${REQUEST_TIMEOUT_MS / 1000}s. The server may be slow or unreachable.`))
    }, REQUEST_TIMEOUT_MS)
    req.write(payload)
    req.end()
  })
}

// Animated spinner shown while a network request is in flight.
function startSpinner(label) {
  const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
  let i = 0
  const isTty = process.stdout.isTTY
  const render = () => {
    if (!isTty) return
    process.stdout.write(`\r${label} ${frames[i = (i + 1) % frames.length]} `)
  }
  render()
  const id = isTty ? setInterval(render, 80) : null
  return () => {
    if (id) clearInterval(id)
    if (isTty) {
      process.stdout.clearLine(0)
      process.stdout.cursorTo(0)
    }
  }
}

async function withSpinner(label, fn) {
  const stop = startSpinner(label)
  try {
    return await fn()
  } finally {
    stop()
  }
}

// ── Readline helpers ─────────────────────────────────────────

function prompt(rl, question) {
  return new Promise(resolve => rl.question(question, resolve))
}

function promptPassword(question) {
  return new Promise(resolve => {
    process.stdout.write(question)
    const stdin = process.stdin
    const wasRaw = stdin.isRaw
    if (stdin.setRawMode) {
      stdin.setRawMode(true)
      stdin.resume()
      let password = ''
      const onData = (ch) => {
        ch = ch.toString()
        if (ch === '\n' || ch === '\r' || ch === '') {
          stdin.setRawMode(wasRaw || false)
          stdin.pause()
          stdin.removeListener('data', onData)
          process.stdout.write('\n')
          if (ch === '') process.exit(1)
          resolve(password)
        } else if (ch === '') {
          if (password.length > 0) {
            password = password.slice(0, -1)
            process.stdout.clearLine(0)
            process.stdout.cursorTo(0)
            process.stdout.write(question + '•'.repeat(password.length))
          }
        } else {
          password += ch
          process.stdout.write('•')
        }
      }
      stdin.on('data', onData)
    } else {
      // fallback (no TTY): read normally
      const rl2 = readline.createInterface({ input: process.stdin, output: null, terminal: false })
      rl2.once('line', (line) => { rl2.close(); resolve(line) })
    }
  })
}

// ── Claude Code MCP config ───────────────────────────────────
//
// Claude Code reads user-scoped MCP servers from ~/.claude.json (NOT
// ~/.claude/settings.json — that file holds general settings only).
// The canonical way to register a server is `claude mcp add`; we fall
// back to writing the JSON file directly when the `claude` binary is
// not on PATH.

const CLAUDE_USER_CONFIG_PATH = path.join(os.homedir(), '.claude.json')

function hasClaudeCli() {
  try {
    const r = spawnSync('claude', ['--version'], { stdio: 'ignore' })
    return r.status === 0
  } catch {
    return false
  }
}

function injectViaClaudeCli(apiKey) {
  // Remove first so re-running init updates an existing entry cleanly.
  spawnSync('claude', ['mcp', 'remove', 'aztea', '--scope', 'user'], { stdio: 'ignore' })
  // The claude CLI uses -e for env vars (not --env). All flags must come
  // before the server name; the command + its args go after `--`.
  const args = [
    'mcp', 'add',
    '--scope', 'user',
    '--transport', 'stdio',
    '-e', `AZTEA_API_KEY=${apiKey}`,
    '-e', `AZTEA_BASE_URL=${BASE_URL}`,
    'aztea',
    '--',
    'npx', '-y', 'aztea-cli', 'mcp',
  ]
  execFileSync('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] })
}

function injectViaFile(apiKey) {
  let cfg = {}
  try {
    cfg = JSON.parse(fs.readFileSync(CLAUDE_USER_CONFIG_PATH, 'utf8'))
  } catch {
    cfg = {}
  }
  if (!cfg.mcpServers || typeof cfg.mcpServers !== 'object') cfg.mcpServers = {}
  cfg.mcpServers.aztea = {
    type: 'stdio',
    command: 'npx',
    args: ['-y', 'aztea-cli', 'mcp'],
    env: {
      AZTEA_API_KEY: apiKey,
      AZTEA_BASE_URL: BASE_URL,
    },
  }
  fs.writeFileSync(CLAUDE_USER_CONFIG_PATH, JSON.stringify(cfg, null, 2) + '\n', 'utf8')
}

function injectMcpConfig(apiKey) {
  if (hasClaudeCli()) {
    try {
      injectViaClaudeCli(apiKey)
      return { method: 'claude mcp add', path: '~/.claude.json (managed by claude)' }
    } catch (err) {
      // claude CLI exists but the command failed (flag mismatch, version
      // skew, etc.). Fall through to direct file write so install never
      // breaks just because the CLI shape changed.
      console.warn(`(claude mcp add failed: ${err.message.split('\n')[0]} — falling back to direct file write)`)
    }
  }
  injectViaFile(apiKey)
  return { method: 'direct write', path: CLAUDE_USER_CONFIG_PATH }
}

// ── Stale config cleanup ─────────────────────────────────────
//
// Earlier CLI versions wrote the MCP config to ~/.claude/settings.json
// (wrong file — Claude Code never read MCP servers from there). Strip
// the legacy entry so users upgrading from <0.4.0 don't end up with
// two conflicting "aztea" registrations.
function removeLegacyMcpEntry() {
  const legacyPath = path.join(os.homedir(), '.claude', 'settings.json')
  try {
    const settings = JSON.parse(fs.readFileSync(legacyPath, 'utf8'))
    if (settings.mcpServers && settings.mcpServers.aztea) {
      delete settings.mcpServers.aztea
      if (Object.keys(settings.mcpServers).length === 0) delete settings.mcpServers
      fs.writeFileSync(legacyPath, JSON.stringify(settings, null, 2) + '\n', 'utf8')
    }
  } catch {
    // file missing or unparseable → nothing to clean
  }
}

// ── Main ─────────────────────────────────────────────────────

async function run() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout })

  console.log('\n' + '─'.repeat(52))
  console.log('  Aztea — agent marketplace for Claude Code')
  console.log('─'.repeat(52) + '\n')

  const hasAccount = await prompt(rl, 'Do you already have an Aztea account? (y/n): ')
  const isExisting = hasAccount.trim().toLowerCase().startsWith('y')

  let apiKey

  if (isExisting) {
    // ── Login ──
    console.log()
    const email = (await prompt(rl, 'Email: ')).trim()
    rl.close()
    const password = await promptPassword('Password: ')

    console.log()
    let res
    try {
      res = await withSpinner('Signing in', () => post(`${BASE_URL}/auth/login`, { email, password }))
    } catch (err) {
      console.error(`Failed to connect to ${BASE_URL}: ${err.message}`)
      console.error('Check your internet connection, or try again — the server may be temporarily slow.')
      process.exit(1)
    }

    if (res.status === 401) {
      console.error('Invalid email or password. Try again, or reset at https://aztea.ai.')
      process.exit(1)
    }
    if (res.status === 403) {
      const detail = typeof res.body === 'object' ? (res.body.detail || res.body.message) : res.body
      console.error(`Sign-in blocked: ${detail}`)
      process.exit(1)
    }
    if (res.status >= 500) {
      console.error(`Server error (HTTP ${res.status}). Please try again in a moment.`)
      process.exit(1)
    }
    if (res.status !== 200) {
      console.error(`Login failed (HTTP ${res.status}): ${JSON.stringify(res.body)}`)
      process.exit(1)
    }
    apiKey = res.body.raw_api_key ?? res.body.api_key
    if (!apiKey) {
      console.error('Server did not return an API key. Please try again or visit https://aztea.ai.')
      process.exit(1)
    }
    console.log('✓ Signed in')
  } else {
    // ── Register ──
    console.log()
    console.log('Creating your account (free, no card required).')
    console.log()
    const username = (await prompt(rl, 'Username: ')).trim()
    const email = (await prompt(rl, 'Email: ')).trim()
    rl.close()
    const password = await promptPassword('Password: ')

    console.log()
    let res
    try {
      res = await withSpinner('Creating account', () => post(`${BASE_URL}/auth/register`, { username, email, password, role: 'hirer' }))
    } catch (err) {
      console.error(`Failed to connect to ${BASE_URL}: ${err.message}`)
      console.error('Check your internet connection, or try again — the server may be temporarily slow.')
      process.exit(1)
    }

    if (res.status === 400) {
      const detail = typeof res.body === 'object' ? (res.body.detail || res.body.message) : res.body
      console.error(`Registration failed: ${detail}`)
      process.exit(1)
    }
    if (res.status >= 500) {
      console.error(`Server error (HTTP ${res.status}). Please try again in a moment.`)
      process.exit(1)
    }
    if (res.status !== 201) {
      console.error(`Registration failed (HTTP ${res.status}): ${JSON.stringify(res.body)}`)
      process.exit(1)
    }
    apiKey = res.body.raw_api_key ?? res.body.api_key
    if (!apiKey) {
      console.error('Server did not return an API key. Please try again or visit https://aztea.ai.')
      process.exit(1)
    }
    const credit = res.body.balance_cents != null
      ? `$${(res.body.balance_cents / 100).toFixed(2)}`
      : '$2.00'
    console.log(`✓ Account created — ${credit} free credit applied, no card needed`)
  }

  // ── Write config ──
  removeLegacyMcpEntry()
  let result
  try {
    result = injectMcpConfig(apiKey)
    console.log(`✓ Added Aztea to Claude Code (${result.method})`)
  } catch (err) {
    console.error(`Could not register MCP server: ${err.message}`)
    console.log('\nAdd this manually to ~/.claude.json:')
    console.log(JSON.stringify({
      mcpServers: {
        aztea: {
          type: 'stdio',
          command: 'npx', args: ['-y', 'aztea-cli', 'mcp'],
          env: { AZTEA_API_KEY: apiKey, AZTEA_BASE_URL: BASE_URL },
        }
      }
    }, null, 2))
    process.exit(1)
  }

  console.log()
  console.log('─'.repeat(52))
  console.log("  You're ready. Restart Claude Code, then try:")
  console.log()
  console.log('  "Review this file for bugs"')
  console.log('  "Generate tests for src/foo.py"')
  console.log('  "Audit my dependencies for CVEs"')
  console.log('  "Run this Python snippet in a sandbox"')
  console.log()
  console.log('  Verify it loaded:    claude mcp list')
  console.log(`  Browse agents:       ${BASE_URL}/agents`)
  console.log('─'.repeat(52))
  console.log()
}

module.exports = { run }
