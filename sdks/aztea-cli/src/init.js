'use strict'

const https = require('https')
const http = require('http')
const fs = require('fs')
const path = require('path')
const os = require('os')
const readline = require('readline')

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
        'User-Agent': 'aztea-cli/0.3.0',
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

// ── Settings file helpers ────────────────────────────────────

const CLAUDE_SETTINGS_PATH = path.join(os.homedir(), '.claude', 'settings.json')

function readSettings() {
  try {
    return JSON.parse(fs.readFileSync(CLAUDE_SETTINGS_PATH, 'utf8'))
  } catch {
    return {}
  }
}

function writeSettings(obj) {
  const dir = path.dirname(CLAUDE_SETTINGS_PATH)
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(CLAUDE_SETTINGS_PATH, JSON.stringify(obj, null, 2) + '\n', 'utf8')
}

function injectMcpConfig(apiKey) {
  const settings = readSettings()
  if (!settings.mcpServers) settings.mcpServers = {}
  settings.mcpServers.aztea = {
    command: 'npx',
    args: ['-y', 'aztea-cli', 'mcp'],
    env: {
      AZTEA_API_KEY: apiKey,
      AZTEA_BASE_URL: BASE_URL,
    },
  }
  writeSettings(settings)
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
  try {
    injectMcpConfig(apiKey)
    console.log(`✓ Added Aztea to Claude Code (${CLAUDE_SETTINGS_PATH})`)
  } catch (err) {
    console.error(`Could not write to ${CLAUDE_SETTINGS_PATH}: ${err.message}`)
    console.log('\nAdd this manually to ~/.claude/settings.json:')
    console.log(JSON.stringify({
      mcpServers: {
        aztea: {
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
  console.log('  "Use Aztea to review this code for bugs"')
  console.log('  "Use Aztea to run this Python snippet"')
  console.log('  "Use Aztea to look up CVEs in express 4.18"')
  console.log()
  console.log(`  Browse all tools: ${BASE_URL}/agents`)
  console.log('─'.repeat(52))
  console.log()
}

module.exports = { run }
