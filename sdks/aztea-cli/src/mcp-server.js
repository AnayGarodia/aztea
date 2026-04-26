'use strict'
/**
 * Lightweight Node.js MCP stdio server for Aztea.
 * Called via: npx aztea-cli mcp  (registered in ~/.claude.json by `claude mcp add`)
 *
 * Reads AZTEA_API_KEY and AZTEA_BASE_URL from env.
 * Fetches the tool list from GET /registry/agents, refreshes every 60s.
 * Proxies tool calls to POST /registry/agents/{id}/call.
 */

const https = require('https')
const http = require('http')

const BASE_URL = (process.env.AZTEA_BASE_URL || 'https://aztea.ai').replace(/\/$/, '')
const API_KEY = process.env.AZTEA_API_KEY || ''
const REFRESH_MS = parseInt(process.env.AZTEA_MCP_REFRESH_SECONDS || '60', 10) * 1000
const TIMEOUT_MS = parseFloat(process.env.AZTEA_MCP_TIMEOUT_SECONDS || '30') * 1000

const AUTH_TOOL = {
  name: 'aztea_setup',
  description: 'Aztea requires an API key. Run `npx aztea-cli init` in your terminal to set one up (free, takes 60 seconds).',
  inputSchema: { type: 'object', properties: {}, required: [] },
}

// Server-level instructions show up alongside the tool list and tell Claude
// when to reach for these tools proactively. Keep this short and action-
// oriented — it's the "elevator pitch" Claude reads before deciding to call.
const SERVER_INSTRUCTIONS = [
  'Aztea is a pay-per-call tool catalog for Claude Code.',
  'Each tool runs a real external operation: live API queries, sandboxed code execution,',
  'image generation, web fetching, and more. Charges are small and refunded on failure.',
  'Call the tool that best matches the task — check the tool description for what each one does.',
].join('\n')

// ── HTTP ─────────────────────────────────────────────────────

function request(method, path, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const url = new URL(BASE_URL + path)
    const lib = url.protocol === 'https:' ? https : http
    const payload = body ? JSON.stringify(body) : null
    const headers = {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json',
      'User-Agent': 'aztea-mcp/0.12.0',
    }
    if (payload) headers['Content-Length'] = Buffer.byteLength(payload)

    const options = {
      hostname: url.hostname,
      port: url.port || (url.protocol === 'https:' ? 443 : 80),
      path: url.pathname + (url.search || ''),
      method,
      headers,
    }

    const req = lib.request(options, (res) => {
      let data = ''
      res.on('data', d => { data += d })
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data) }) }
        catch { resolve({ status: res.statusCode, body: data }) }
      })
    })
    req.setTimeout(timeoutMs || TIMEOUT_MS, () => { req.destroy(new Error('timeout')) })
    req.on('error', reject)
    if (payload) req.write(payload)
    req.end()
  })
}

// ── Tool description builder ─────────────────────────────────
// Keep descriptions short — tools/list is loaded into context every
// session. A one-liner per tool beats a paragraph.
function buildToolDescription(agent) {
  const hint = inferUseWhenHint(agent)
  const price = agent.price_per_call_usd != null
    ? `$${Number(agent.price_per_call_usd).toFixed(2)}/call`
    : ''
  return [hint, price].filter(Boolean).join(' · ')
}

function inferUseWhenHint(agent) {
  const name = String(agent.name || '').toLowerCase()
  const desc = String(agent.description || '').toLowerCase()
  const all = `${name} ${desc}`
  if (/python (executor|runner)|sandbox/.test(all)) return 'Use when the task requires actually running Python code'
  if (/cve|nvd/.test(all)) return 'Use for CVE and vulnerability lookups against live NVD data'
  if (/dependency.*audit|audit.*depend/.test(all)) return 'Use to audit a requirements.txt or package.json for known vulnerabilities'
  if (/arxiv|preprint/.test(all)) return 'Use when searching for academic papers on arXiv'
  if (/financial|sec|ticker|edgar/.test(all)) return 'Use when looking up public company financials or SEC filings'
  if (/hacker.?news|hn digest/.test(all)) return 'Use when you want the current Hacker News front page'
  if (/web (research|fetch)|url/.test(all)) return 'Use when the task requires fetching and reading a live URL'
  if (/github.*(fetch|file|repo)/.test(all)) return 'Use to read files from a public GitHub repo'
  if (/image (gen|generator)|draw|illustration/.test(all)) return 'Use when the task requires generating an image'
  if (/code review|reviewer/.test(all)) return 'Use for a dedicated code review pass with structured findings'
  if (/test (gen|writer)|generate test/.test(all)) return 'Use when generating a test suite for a code snippet'
  if (/spec writer|specification/.test(all)) return 'Use when producing a formal spec, PRD, or RFC document'
  if (/pr review/.test(all)) return 'Use to review a GitHub pull request by URL'
  if (/wiki/.test(all)) return 'Use when researching a topic with structured Wikipedia extraction'
  return 'Use this Aztea tool when the task matches its description'
}

// ── Registry ─────────────────────────────────────────────────

let _tools = []         // MCP tool descriptors
let _toolMap = {}       // tool_name → agent_id
let _authRequired = !API_KEY
let _initialRefreshDone = false

function notifyToolsChanged() {
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/tools/list_changed' }) + '\n')
}

async function refresh() {
  if (_authRequired) return
  try {
    const res = await request('GET', '/registry/agents?include_reputation=false', null, 10000)
    if (res.status === 401 || res.status === 403) { _authRequired = true; return }
    const agents = Array.isArray(res.body?.agents) ? res.body.agents : []
    const tools = []
    const map = {}
    for (const a of agents) {
      const name = (a.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '').slice(0, 60)
      if (!name || !a.agent_id) continue
      const description = buildToolDescription(a)
      const tool = {
        name,
        description,
        inputSchema: (a.input_schema && typeof a.input_schema === 'object' && a.input_schema.type)
          ? a.input_schema
          : { type: 'object', properties: { task: { type: 'string', description: 'What to do' } }, required: ['task'] },
      }
      tools.push(tool)
      map[name] = a.agent_id
    }
    _tools = tools
    _toolMap = map
    _authRequired = false
    // Notify Claude Code to re-fetch the tool list. On first load this
    // replaces the empty AUTH_TOOL placeholder with the real catalog.
    if (!_initialRefreshDone) {
      _initialRefreshDone = true
      notifyToolsChanged()
    }
  } catch (err) {
    log(`Registry refresh failed: ${err.message}`)
  }
}

function getTools() {
  if (_authRequired || !API_KEY) return [AUTH_TOOL]
  return _tools.length ? _tools : [AUTH_TOOL]
}

async function callTool(name, args) {
  if (_authRequired || !API_KEY) {
    return { isError: true, content: [{ type: 'text', text: 'Run `npx aztea-cli init` to set up your API key.' }] }
  }
  const agentId = _toolMap[name]
  if (!agentId) {
    return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${name}` }] }
  }
  try {
    const res = await request('POST', `/registry/agents/${agentId}/call`, args || {})
    if (res.status === 401 || res.status === 403) {
      _authRequired = true
      return { isError: true, content: [{ type: 'text', text: 'API key invalid. Run `npx aztea-cli init` to update.' }] }
    }
    const body = res.body
    const text = typeof body === 'string'
      ? body
      : (body?.summary || body?.message || body?.answer || body?.result || body?.output || JSON.stringify(body, null, 2))
    const content = [{ type: 'text', text: String(text) }]
    // pass through image artifacts
    if (Array.isArray(body?.artifacts)) {
      for (const a of body.artifacts.slice(0, 4)) {
        const src = a?.url_or_base64 || ''
        const mime = a?.mime || ''
        if (src.startsWith('data:image/') && src.includes(';base64,')) {
          const [, b64] = src.split(';base64,')
          content.push({ type: 'image', mimeType: mime || 'image/png', data: b64 })
        }
      }
    }
    return { isError: !res.body || res.status >= 400, content }
  } catch (err) {
    return { isError: true, content: [{ type: 'text', text: `Tool call failed: ${err.message}` }] }
  }
}

// ── stdio JSON-RPC ────────────────────────────────────────────

function log(msg) {
  process.stderr.write(`[aztea-mcp] ${msg}\n`)
}

function writeMsg(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

function readMessages() {
  let buf = ''
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', (chunk) => {
    buf += chunk
    let nl
    while ((nl = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, nl).replace(/\r$/, '')
      buf = buf.slice(nl + 1)
      if (!line.trim()) continue
      let msg
      try { msg = JSON.parse(line) } catch { continue }
      handleMessage(msg)
    }
  })
  process.stdin.on('end', () => process.exit(0))
}

async function handleMessage(msg) {
  if (!msg || typeof msg !== 'object' || !('id' in msg)) return
  const { id, method, params } = msg
  const reply = (result) => writeMsg({ jsonrpc: '2.0', id, result })
  const error = (code, message) => writeMsg({ jsonrpc: '2.0', id, error: { code, message } })

  if (method === 'initialize') {
    return reply({
      protocolVersion: '2024-11-05',
      capabilities: { tools: { listChanged: true } },
      serverInfo: { name: 'aztea-registry-mcp', version: '0.3.0' },
      instructions: SERVER_INSTRUCTIONS,
    })
  }
  if (method === 'ping') return reply({})
  if (method === 'tools/list') return reply({ tools: getTools() })
  if (method === 'tools/call') {
    if (!params || typeof params !== 'object') return error(-32602, 'params required')
    const name = String(params.name || '').trim()
    const args = (params.arguments && typeof params.arguments === 'object') ? params.arguments : {}
    if (!name) return error(-32602, 'name required')
    const result = await callTool(name, args)
    return reply(result)
  }
  return error(-32601, `Method '${method}' not found`)
}

// ── Entry ─────────────────────────────────────────────────────

function run() {
  if (!API_KEY) {
    log('No AZTEA_API_KEY set — run `npx aztea-cli init` to configure.')
  }
  // Start listening immediately so the initialize handshake is instant.
  // The registry refresh runs in the background; once done it fires
  // notifications/tools/list_changed so Claude Code re-fetches the
  // real catalog instead of the placeholder AUTH_TOOL.
  readMessages()
  refresh().catch(err => log(`initial refresh failed: ${err.message}`))
  setInterval(refresh, REFRESH_MS)
}

module.exports = { run }
if (require.main === module) run()
