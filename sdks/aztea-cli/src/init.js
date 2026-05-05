'use strict'

const https = require('https')
const http = require('http')
const fs = require('fs')
const path = require('path')
const os = require('os')
const readline = require('readline')
const { execFileSync, spawnSync } = require('child_process')

const BASE_URL = process.env.AZTEA_BASE_URL || 'https://aztea.ai'
const USE_COLOR = process.stdout.isTTY && process.env.NO_COLOR == null
const ANSI = {
  reset: '\x1b[0m',
  dim: '\x1b[2m',
  bold: '\x1b[1m',
  teal: '\x1b[36m',
  green: '\x1b[32m',
  amber: '\x1b[33m',
  violet: '\x1b[35m',
}

function c(color, text) {
  return USE_COLOR ? `${ANSI[color]}${text}${ANSI.reset}` : text
}

function ok(text) {
  console.log(`${c('green', '✓')} ${text}`)
}

function muted(text) {
  return c('dim', text)
}

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
        'User-Agent': 'aztea-cli/0.17.5',
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
    if (!process.stdin.isTTY || !process.stdout.isTTY) {
      const rl2 = readline.createInterface({ input: process.stdin, output: null, terminal: false })
      rl2.once('line', (line) => { rl2.close(); resolve(line) })
      return
    }

    const rl2 = readline.createInterface({
      input: process.stdin,
      output: process.stdout,
      terminal: true,
    })

    let currentValue = ''
    rl2.stdoutMuted = true
    rl2._writeToOutput = function _writeToOutput(stringToWrite) {
      if (!rl2.stdoutMuted) {
        rl2.output.write(stringToWrite)
        return
      }
      if (stringToWrite === '\n' || stringToWrite === '\r\n') {
        rl2.output.write(stringToWrite)
        return
      }
      rl2.output.write(`\r${question}${'*'.repeat(currentValue.length)}`)
    }

    rl2.question(question, (value) => {
      currentValue = ''
      rl2.close()
      resolve(value)
    })

    rl2.input.on('data', (chunk) => {
      const text = chunk.toString('utf8')
      if (text === '\r' || text === '\n') return
      if (text === '\u0003') return
      if (text === '\u007f') {
        currentValue = currentValue.slice(0, -1)
        return
      }
      if (text === '\u0015') {
        currentValue = ''
        return
      }
      currentValue += text
    })
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

// Claude Code spawns the MCP server with a short startup timeout.
// `npx -y aztea-cli mcp` is slow because npx re-resolves the package
// on every spawn. Instead we install aztea-cli to ~/.aztea/ (no sudo
// needed) and point Claude Code at `node ~/.aztea/.../mcp-server.js`
// directly — instant startup every time.
const AZTEA_LOCAL_DIR = path.join(os.homedir(), '.aztea')

function mcpServerConfig(apiKey, mcpScript) {
  return mcpScript
    ? {
        type: 'stdio',
        command: process.execPath,
        args: [mcpScript],
        env: {
          AZTEA_API_KEY: apiKey,
          AZTEA_BASE_URL: BASE_URL,
          AZTEA_CLIENT_ID: 'coding-agent',
        },
      }
    : {
        type: 'stdio',
        command: 'npx',
        args: ['-y', 'aztea-cli', 'mcp'],
        env: {
          AZTEA_API_KEY: apiKey,
          AZTEA_BASE_URL: BASE_URL,
          AZTEA_CLIENT_ID: 'coding-agent',
        },
      }
}

function ensureLocalInstall() {
  fs.mkdirSync(AZTEA_LOCAL_DIR, { recursive: true })
  const r = spawnSync('npm', ['install', '--silent', '--prefix', AZTEA_LOCAL_DIR, 'aztea-cli@latest'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  if (r.status !== 0) {
    const err = (r.stderr ? r.stderr.toString() : '').trim().split('\n').pop()
    throw new Error(`npm install aztea-cli to ~/.aztea failed: ${err || 'unknown error'}`)
  }
  // Return path to the installed mcp-server.js
  const mcpScript = path.join(AZTEA_LOCAL_DIR, 'node_modules', 'aztea-cli', 'src', 'mcp-server.js')
  if (!fs.existsSync(mcpScript)) throw new Error('mcp-server.js not found after install')
  return mcpScript
}

function injectViaClaudeCli(apiKey, mcpScript) {
  // Try removing any existing entry first (ignore errors — flag may differ by version)
  spawnSync('claude', ['mcp', 'remove', 'aztea'], { stdio: 'ignore' })
  spawnSync('claude', ['mcp', 'remove', 'aztea', '--scope', 'user'], { stdio: 'ignore' })

  const nodeExe = process.execPath
  const mcpArgs = mcpScript
    ? [nodeExe, mcpScript]
    : ['npx', '-y', 'aztea-cli', 'mcp']

  // Try with --scope user first, then without (older Claude Code versions).
  // NOTE: name must come before -e flags — the variadic -e option otherwise
  // consumes the server name as an env-var value and the command fails.
  for (const scopeArgs of [['--scope', 'user'], []]) {
    const args = [
      'mcp', 'add',
      ...scopeArgs,
      '--transport', 'stdio',
      'aztea',
      '-e', `AZTEA_API_KEY=${apiKey}`,
      '-e', `AZTEA_BASE_URL=${BASE_URL}`,
      '--',
      ...mcpArgs,
    ]
    try {
      execFileSync('claude', args, { stdio: ['ignore', 'pipe', 'pipe'] })
      return
    } catch {
      // try next variant
    }
  }
  throw new Error('all claude mcp add variants failed')
}

function injectViaFile(apiKey, mcpScript) {
  let cfg = {}
  try {
    cfg = JSON.parse(fs.readFileSync(CLAUDE_USER_CONFIG_PATH, 'utf8'))
  } catch {
    cfg = {}
  }
  if (!cfg.mcpServers || typeof cfg.mcpServers !== 'object') cfg.mcpServers = {}
  cfg.mcpServers.aztea = mcpScript
    ? mcpServerConfig(apiKey, mcpScript)
    : mcpServerConfig(apiKey, null)
  fs.writeFileSync(CLAUDE_USER_CONFIG_PATH, JSON.stringify(cfg, null, 2) + '\n', 'utf8')
}

function writePortableAgentConfigs(apiKey, mcpScript) {
  fs.mkdirSync(AZTEA_LOCAL_DIR, { recursive: true })
  const config = { mcpServers: { aztea: mcpServerConfig(apiKey, mcpScript) } }
  const portablePath = path.join(AZTEA_LOCAL_DIR, 'mcp.json')
  fs.writeFileSync(portablePath, JSON.stringify(config, null, 2) + '\n', 'utf8')

  const guidePath = path.join(AZTEA_LOCAL_DIR, 'coding-agent-setup.md')
  fs.writeFileSync(guidePath, [
    '# Aztea MCP setup',
    '',
    'Aztea exposes one portable stdio MCP server. Point Codex, Cursor, Gemini CLI, or any MCP host at this config:',
    '',
    `\`${portablePath}\``,
    '',
    'The important behavior is in the server instructions: the coding agent should call `aztea_do` proactively when a specialist hire is useful. The user should not need to type "use Aztea".',
    '',
    'Server config:',
    '',
    '```json',
    JSON.stringify(config, null, 2),
    '```',
    '',
  ].join('\n'), 'utf8')

  return { portablePath, guidePath }
}

function injectMcpConfig(apiKey) {
  // Install to ~/.aztea/ so Claude Code can spawn `node mcp-server.js`
  // directly — no npx delay, no sudo required.
  let mcpScript = null
  try {
    mcpScript = ensureLocalInstall()
  } catch (err) {
    console.warn(`(could not install aztea-cli to ~/.aztea: ${err.message} — falling back to npx)`)
  }

  if (hasClaudeCli()) {
    try {
      injectViaClaudeCli(apiKey, mcpScript)
      const portable = writePortableAgentConfigs(apiKey, mcpScript)
      return { method: 'claude mcp add', path: '~/.claude.json (managed by claude)', ...portable }
    } catch (err) {
      console.warn(`(claude mcp add failed — falling back to direct file write)`)
    }
  }
  injectViaFile(apiKey, mcpScript)
  const portable = writePortableAgentConfigs(apiKey, mcpScript)
  return { method: 'direct write', path: CLAUDE_USER_CONFIG_PATH, ...portable }
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

  console.log('\n' + c('teal', '━'.repeat(56)))
  console.log(`  ${c('bold', 'Aztea')} ${muted('for coding agents')}`)
  console.log(`  ${c('amber', 'Hire specialists. Cap spend. Verify receipts.')}`)
  console.log(c('teal', '━'.repeat(56)) + '\n')

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
      res = await withSpinner('Signing in', () => post(`${BASE_URL}/auth/login`, { email, password, rotate: true }))
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
    ok('Signed in')
  } else {
    // ── Register ──
    console.log()
    console.log(`${c('bold', 'Create your account')} ${muted('(free, no card required).')}`)
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
    ok(`Account created — ${credit} starter credit applied, no card needed`)
  }

  // ── Write config ──
  removeLegacyMcpEntry()
  let result
  try {
    result = injectMcpConfig(apiKey)
    ok(`Claude Code configured (${result.method})`)
    ok(`Portable MCP config written for Codex, Cursor, Gemini, and other MCP hosts`)
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
  console.log(c('teal', '━'.repeat(56)))
  console.log(`  ${c('bold', 'Ready for the first hire')}`)
  console.log(`  Restart Claude Code. For other MCP hosts, import: ${c('violet', result.portablePath)}`)
  console.log()
  console.log(`  ${muted('Try normal coding-agent prompts. Do not say "use Aztea".')}`)
  console.log('  "Before I deploy, check this API for latency and obvious risk."')
  console.log('  "Audit this package list for known vulnerabilities."')
  console.log('  "Run this repro script and tell me what actually happens."')
  console.log()
  console.log(`  Verify Claude:       ${c('violet', 'claude mcp list')}`)
  console.log(`  Other agents guide:  ${c('violet', result.guidePath)}`)
  console.log(`  Browse agents:       ${c('violet', `${BASE_URL}/agents`)}`)
  console.log(c('teal', '━'.repeat(56)))
  console.log()
}

async function loginWithKey(apiKey) {
  if (!apiKey || !apiKey.startsWith('az_')) {
    console.error('Invalid API key — expected an az_... key.')
    process.exit(1)
  }
  removeLegacyMcpEntry()
  let result
  try {
    result = injectMcpConfig(apiKey)
  } catch (err) {
    console.error(`Could not register MCP server: ${err.message}`)
    process.exit(1)
  }
  console.log(`\n${c('teal', '━'.repeat(56))}`)
  console.log(`  ${c('bold', 'Aztea')} ${muted('logged in')}`)
  console.log(c('teal', '━'.repeat(56)))
  ok(`API key configured (${result.method})`)
  ok(`Portable MCP config written: ${result.portablePath}`)
  console.log('\nRestart Claude Code to apply the new key.')
  console.log(`Browse agents: ${BASE_URL}/agents`)
  console.log()
}

async function whoami() {
  const cfgPath = path.join(os.homedir(), '.claude.json')
  try {
    const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'))
    const env = cfg.mcpServers && cfg.mcpServers.aztea && cfg.mcpServers.aztea.env
    const key = env && env.AZTEA_API_KEY
    if (!key) {
      console.log('No Aztea API key configured. Run: npx -y aztea-cli@latest init')
      return
    }
    const prefix = key.slice(0, 10) + '...'
    console.log(`API key: ${prefix}`)
    console.log(`Server:  ${(env.AZTEA_BASE_URL || 'https://aztea.ai')}`)
  } catch {
    console.log('No Aztea config found. Run: npx -y aztea-cli@latest init')
  }
}

module.exports = { run, loginWithKey, whoami }
