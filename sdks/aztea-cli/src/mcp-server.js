'use strict'
/**
 * Lightweight Node.js MCP stdio server for Aztea.
 * Called via: npx aztea mcp  (or configured in ~/.claude/settings.json)
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
  description: 'Aztea requires an API key. Run `npx aztea init` in your terminal to set one up (free, takes 60 seconds).',
  inputSchema: { type: 'object', properties: {}, required: [] },
}

// ── HTTP ─────────────────────────────────────────────────────

function request(method, path, body, timeoutMs) {
  return new Promise((resolve, reject) => {
    const url = new URL(BASE_URL + path)
    const lib = url.protocol === 'https:' ? https : http
    const payload = body ? JSON.stringify(body) : null
    const headers = {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json',
      'User-Agent': 'aztea-mcp/0.2.0',
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

// ── Registry ─────────────────────────────────────────────────

let _tools = []         // MCP tool descriptors
let _toolMap = {}       // tool_name → agent_id
let _authRequired = !API_KEY

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
      const tool = {
        name,
        description: [
          a.description || '',
          a.price_per_call_usd != null ? `Price: $${Number(a.price_per_call_usd).toFixed(4)}/call` : '',
        ].filter(Boolean).join(' — '),
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
    return { isError: true, content: [{ type: 'text', text: 'Run `npx aztea init` to set up your API key.' }] }
  }
  const agentId = _toolMap[name]
  if (!agentId) {
    return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${name}` }] }
  }
  try {
    const res = await request('POST', `/registry/agents/${agentId}/call`, args || {})
    if (res.status === 401 || res.status === 403) {
      _authRequired = true
      return { isError: true, content: [{ type: 'text', text: 'API key invalid. Run `npx aztea init` to update.' }] }
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
  const encoded = Buffer.from(JSON.stringify(obj), 'utf8')
  process.stdout.write(`Content-Length: ${encoded.length}\r\n\r\n`)
  process.stdout.write(encoded)
}

function readMessages() {
  let buf = Buffer.alloc(0)
  process.stdin.on('data', (chunk) => {
    buf = Buffer.concat([buf, chunk])
    while (true) {
      const headerEnd = buf.indexOf('\r\n\r\n')
      if (headerEnd === -1) break
      const headerStr = buf.slice(0, headerEnd).toString('utf8')
      const clMatch = headerStr.match(/Content-Length:\s*(\d+)/i)
      if (!clMatch) { buf = buf.slice(headerEnd + 4); continue }
      const cl = parseInt(clMatch[1], 10)
      const bodyStart = headerEnd + 4
      if (buf.length < bodyStart + cl) break
      const body = buf.slice(bodyStart, bodyStart + cl).toString('utf8')
      buf = buf.slice(bodyStart + cl)
      let msg
      try { msg = JSON.parse(body) } catch { continue }
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
      capabilities: { tools: { listChanged: false } },
      serverInfo: { name: 'aztea-registry-mcp', version: '0.2.0' },
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

async function run() {
  if (!API_KEY) {
    log('No AZTEA_API_KEY set — run `npx aztea init` to configure.')
  }
  await refresh()
  setInterval(refresh, REFRESH_MS)
  readMessages()
}

module.exports = { run }
if (require.main === module) run()
