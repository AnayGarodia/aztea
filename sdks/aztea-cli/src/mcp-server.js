'use strict'
/**
 * Current Aztea MCP stdio server for external users.
 *
 * Design:
 * - expose a compact lazy MCP surface: aztea_search, aztea_describe, aztea_call
 * - hydrate the searchable catalog from the live /codex/tools manifest
 * - route registry-agent calls to /registry/agents/{id}/call
 * - route platform meta-tools to their direct HTTP endpoints
 */

const https = require('https')
const http = require('http')

const BASE_URL = (process.env.AZTEA_BASE_URL || 'https://aztea.ai').replace(/\/$/, '')
const API_KEY = process.env.AZTEA_API_KEY || ''
const CLIENT_ID = (process.env.AZTEA_CLIENT_ID || 'claude-code').trim() || 'claude-code'
const REFRESH_MS = parseInt(process.env.AZTEA_MCP_REFRESH_SECONDS || '60', 10) * 1000
const TIMEOUT_MS = parseFloat(process.env.AZTEA_MCP_TIMEOUT_SECONDS || '30') * 1000
const AZTEA_VERSION = '1.0'
const USER_AGENT = 'aztea-mcp/0.14.0'

const AUTH_TOOL = {
  name: 'aztea_setup',
  description: 'Aztea requires an API key. Run `npx aztea-cli init` in your terminal to set one up.',
  inputSchema: { type: 'object', properties: {}, required: [] },
}

const LAZY_SEARCH_TOOL = {
  name: 'aztea_search',
  description: (
    'Find the right Aztea tool for a task. Call this first when you need live external data, ' +
    'real code execution, vulnerability lookup, SQL execution, web research, endpoint testing, ' +
    'screenshots, semantic repo search, or any other marketplace capability.'
  ),
  inputSchema: {
    type: 'object',
    properties: {
      query: { type: 'string', description: 'Natural-language description of what you want to do.' },
      limit: { type: 'integer', minimum: 1, maximum: 20, default: 8, description: 'Max results to return.' },
    },
    required: ['query'],
  },
}

const LAZY_DESCRIBE_TOOL = {
  name: 'aztea_describe',
  description: 'Get the full input schema and details for an Aztea tool returned by aztea_search.',
  inputSchema: {
    type: 'object',
    properties: {
      slug: { type: 'string', description: 'Tool slug exactly as returned by aztea_search.' },
    },
    required: ['slug'],
  },
}

const LAZY_CALL_TOOL = {
  name: 'aztea_call',
  description: (
    'Invoke any Aztea tool or platform workflow. Workflow: aztea_search -> aztea_describe -> aztea_call. ' +
    "Registry-agent results come back in {job_id, status, output, latency_ms, cached}; the actual tool result is in 'output'."
  ),
  inputSchema: {
    type: 'object',
    properties: {
      slug: { type: 'string', description: 'Tool slug returned by aztea_search.' },
      arguments: { type: 'object', description: 'Arguments matching the schema from aztea_describe.' },
    },
    required: ['slug', 'arguments'],
  },
}

const LAZY_TOOL_NAMES = new Set([LAZY_SEARCH_TOOL.name, LAZY_DESCRIBE_TOOL.name, LAZY_CALL_TOOL.name])

const SERVER_INSTRUCTIONS = [
  'You have access to the Aztea AI agent marketplace.',
  'Use it when a task needs live external data, real code execution, or specialized workflows you cannot do from chat alone.',
  '',
  'Workflow:',
  "1. aztea_search('what you want to do')",
  '2. aztea_describe(slug)',
  '3. aztea_call(slug, {arguments})',
].join('\n')

const SESSION_STATE = {
  budgetCents: null,
  spentCents: 0,
}

const META_TOOL_NAMES = new Set([
  'aztea_wallet_balance',
  'aztea_spend_summary',
  'aztea_set_daily_limit',
  'aztea_topup_url',
  'aztea_session_summary',
  'aztea_set_session_budget',
  'aztea_estimate_cost',
  'aztea_list_recipes',
  'aztea_list_pipelines',
  'aztea_hire_async',
  'aztea_job_status',
  'aztea_clarify',
  'aztea_rate_job',
  'aztea_dispute_job',
  'aztea_verify_output',
  'aztea_discover',
  'aztea_get_examples',
  'aztea_hire_batch',
  'aztea_compare_agents',
  'aztea_compare_status',
  'aztea_select_compare_winner',
  'aztea_run_pipeline',
  'aztea_pipeline_status',
  'aztea_run_recipe',
])

let _catalog = []
let _authRequired = !API_KEY
let _initialRefreshDone = false

function log(msg) {
  process.stderr.write(`[aztea-mcp] ${msg}\n`)
}

function writeMsg(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n')
}

function notifyToolsChanged() {
  writeMsg({ jsonrpc: '2.0', method: 'notifications/tools/list_changed' })
}

function headers(extra = {}) {
  return {
    Authorization: `Bearer ${API_KEY}`,
    'Content-Type': 'application/json',
    'User-Agent': USER_AGENT,
    'X-Aztea-Version': AZTEA_VERSION,
    'X-Aztea-Client': CLIENT_ID,
    ...extra,
  }
}

function request(method, path, body, timeoutMs, extraHeaders = {}) {
  return new Promise((resolve, reject) => {
    const url = new URL(BASE_URL + path)
    const lib = url.protocol === 'https:' ? https : http
    const payload = body == null ? null : JSON.stringify(body)
    const hdrs = headers(extraHeaders)
    if (payload) hdrs['Content-Length'] = Buffer.byteLength(payload)
    const req = lib.request({
      hostname: url.hostname,
      port: url.port || (url.protocol === 'https:' ? 443 : 80),
      path: url.pathname + (url.search || ''),
      method,
      headers: hdrs,
    }, (res) => {
      let data = ''
      res.on('data', chunk => { data += chunk })
      res.on('end', () => {
        try {
          resolve({ status: res.statusCode || 0, body: JSON.parse(data), headers: res.headers })
        } catch {
          resolve({ status: res.statusCode || 0, body: data, headers: res.headers })
        }
      })
    })
    req.setTimeout(timeoutMs || TIMEOUT_MS, () => req.destroy(new Error('timeout')))
    req.on('error', reject)
    if (payload) req.write(payload)
    req.end()
  })
}

function getJson(path) {
  return request('GET', path, null, TIMEOUT_MS)
}

function postJson(path, body) {
  return request('POST', path, body, TIMEOUT_MS)
}

function parseApiResponse(res) {
  const body = typeof res.body === 'object' && res.body !== null ? res.body : { raw_body: String(res.body || '') }
  if (res.status >= 200 && res.status < 300) return { ok: true, body }
  const out = { error: 'API_ERROR', status_code: res.status, ...body }
  const detail = body.detail
  if (detail && typeof detail === 'object') {
    if (detail.message && !out.message) out.message = detail.message
    if (detail.data && typeof detail.data === 'object') {
      for (const key of ['refunded', 'refund_amount_cents', 'cost_usd', 'wallet_balance_cents']) {
        if (detail.data[key] != null && out[key] == null) out[key] = detail.data[key]
      }
    }
  } else if (typeof detail === 'string' && !out.message) {
    out.message = detail
  }
  return { ok: false, body: out }
}

function accumulate(amountCents) {
  if (amountCents == null) return
  SESSION_STATE.spentCents += Number(amountCents) || 0
}

function budgetGuard() {
  if (SESSION_STATE.budgetCents == null) return null
  if (SESSION_STATE.spentCents < SESSION_STATE.budgetCents) return null
  return {
    error: 'SESSION_BUDGET_EXCEEDED',
    message: `Session budget of $${(SESSION_STATE.budgetCents / 100).toFixed(2)} reached.`,
    budget_cents: SESSION_STATE.budgetCents,
    spent_cents: SESSION_STATE.spentCents,
  }
}

function getTools() {
  if (_authRequired || !API_KEY) return [AUTH_TOOL]
  return [LAZY_SEARCH_TOOL, LAZY_DESCRIBE_TOOL, LAZY_CALL_TOOL]
}

function authRequiredResponse() {
  return {
    error: 'AUTHENTICATION_REQUIRED',
    message: 'You need an Aztea API key to call agents.',
    signup_url: `${BASE_URL}/signup`,
    next_step: 'Run `npx aztea-cli init` or set AZTEA_API_KEY=az_... and restart the MCP server.',
  }
}

async function refreshCatalog() {
  if (!API_KEY) return
  try {
    const res = await getJson('/codex/tools')
    if (res.status === 401 || res.status === 403) {
      _authRequired = true
      return
    }
    const parsed = parseApiResponse(res)
    if (!parsed.ok) {
      log(`catalog refresh failed: HTTP ${res.status}`)
      return
    }
    const tools = Array.isArray(parsed.body.tools) ? parsed.body.tools : []
    const lookup = parsed.body.tool_lookup && typeof parsed.body.tool_lookup === 'object' ? parsed.body.tool_lookup : {}
    _catalog = tools
      .filter(tool => tool && tool.type === 'function' && tool.name)
      .map(tool => {
        const meta = lookup[tool.name] || {}
        return {
          slug: String(tool.name).trim(),
          kind: meta.kind || 'registry_agent',
          agent_id: meta.agent_id || null,
          name: String(tool.name).trim(),
          description: String(tool.description || '').trim(),
          inputSchema: tool.parameters && typeof tool.parameters === 'object'
            ? tool.parameters
            : { type: 'object', properties: {}, required: [] },
        }
      })
    _authRequired = false
    if (!_initialRefreshDone) {
      _initialRefreshDone = true
      notifyToolsChanged()
    }
  } catch (err) {
    log(`catalog refresh failed: ${err.message}`)
  }
}

function searchCatalog(query, limit) {
  const normalized = String(query || '').trim().toLowerCase()
  const capped = Math.max(1, Math.min(Number(limit || 8), 20))
  const terms = normalized.split(/\s+/).filter(Boolean)
  const scored = []
  for (const entry of _catalog) {
    const haystack = `${entry.slug}\n${entry.description}`.toLowerCase()
    let score = 0
    if (entry.slug.toLowerCase() === normalized) score += 100
    if (normalized && entry.slug.toLowerCase().includes(normalized)) score += 25
    if (normalized && haystack.includes(normalized)) score += 20
    for (const term of terms) {
      if (haystack.includes(term)) score += 3
    }
    if (score > 0) scored.push({ score, entry })
  }
  scored.sort((a, b) => b.score - a.score)
  const results = scored.slice(0, capped).map(({ score, entry }) => ({
    slug: entry.slug,
    kind: entry.kind,
    agent_id: entry.agent_id,
    description: entry.description.slice(0, 400),
    score,
  }))
  return {
    query,
    count: results.length,
    results,
    next_step: results.length
      ? `Call aztea_describe(slug='${results[0].slug}') to get the full schema, then aztea_call(slug=..., arguments={...}).`
      : 'No matches found. Try a broader query.',
  }
}

function describeCatalog(slug) {
  const entry = _catalog.find(item => item.slug === String(slug || '').trim())
  if (!entry) {
    return { ok: false, payload: { error: 'TOOL_NOT_FOUND', message: `Unknown tool '${slug}'.`, hint: 'Use aztea_search first.' } }
  }
  return {
    ok: true,
    payload: {
      slug: entry.slug,
      kind: entry.kind,
      agent_id: entry.agent_id,
      description: entry.description,
      input_schema: entry.inputSchema,
      next_step: `Call aztea_call(slug='${entry.slug}', arguments={...}) with fields from input_schema above.`,
    },
  }
}

async function walletBalance() {
  return parseApiResponse(await getJson('/wallets/me'))
}

async function spendSummary(args) {
  let period = String(args.period || '7d')
  if (!['1d', '7d', '30d', '90d'].includes(period)) period = '7d'
  return parseApiResponse(await getJson(`/wallets/spend-summary?period=${encodeURIComponent(period)}`))
}

async function setDailyLimit(args) {
  const limit = Number(args.limit_cents || 0)
  return parseApiResponse(await postJson('/wallets/me/daily-spend-limit', {
    daily_spend_limit_cents: limit > 0 ? limit : null,
  }))
}

async function topupUrl(args) {
  const amount = Number(args.amount_cents || 500)
  if (!(amount >= 100 && amount <= 50000)) {
    return { ok: false, body: { error: 'INVALID_INPUT', message: 'amount_cents must be 100-50000.' } }
  }
  const walletRes = await walletBalance()
  if (!walletRes.ok) return walletRes
  const walletId = walletRes.body.wallet_id
  if (!walletId) return { ok: false, body: { error: 'WALLET_FETCH_FAILED', message: 'wallet_id not found.' } }
  const res = parseApiResponse(await postJson('/wallets/topup/session', { wallet_id: walletId, amount_cents: amount }))
  if (res.ok && !res.body.note) res.body.note = 'Open checkout_url in a browser to complete payment.'
  return res
}

async function sessionSummary() {
  const [bal, spend] = await Promise.all([
    walletBalance(),
    parseApiResponse(await getJson('/wallets/spend-summary?period=1d')),
  ])
  const result = {
    session_spent_cents: SESSION_STATE.spentCents,
    session_spent_usd: Number((SESSION_STATE.spentCents / 100).toFixed(4)),
    session_budget_cents: SESSION_STATE.budgetCents,
    session_budget_usd: SESSION_STATE.budgetCents == null ? null : Number((SESSION_STATE.budgetCents / 100).toFixed(4)),
  }
  if (bal.ok) {
    result.balance_cents = bal.body.balance_cents
    result.balance_usd = Number(((Number(bal.body.balance_cents || 0)) / 100).toFixed(4))
  }
  if (spend.ok) {
    result.today_spend_cents = spend.body.total_cents
    result.today_jobs = spend.body.total_jobs
    result.today_by_agent = spend.body.by_agent
  }
  return { ok: true, body: result }
}

async function estimateCost(args) {
  const agentId = String(args.agent_id || '').trim()
  if (!agentId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'agent_id is required.' } }
  const input = args.input_payload == null ? {} : args.input_payload
  if (typeof input !== 'object' || Array.isArray(input)) {
    return { ok: false, body: { error: 'INVALID_INPUT', message: 'input_payload must be an object.' } }
  }
  const res = parseApiResponse(await postJson(`/agents/${agentId}/estimate`, input))
  if (res.ok && !res.body.note) res.body.note = 'This is a preview only. No charge has been applied.'
  return res
}

async function listRecipes() {
  const res = parseApiResponse(await getJson('/recipes'))
  if (res.ok) {
    const recipes = Array.isArray(res.body.recipes) ? res.body.recipes : []
    if (res.body.count == null) res.body.count = recipes.length
    if (!res.body.note) res.body.note = 'Use recipe_id with aztea_run_recipe to execute one of these workflows.'
  }
  return res
}

async function listPipelines() {
  const res = parseApiResponse(await getJson('/pipelines'))
  if (res.ok) {
    const pipelines = Array.isArray(res.body.pipelines) ? res.body.pipelines : []
    if (res.body.count == null) res.body.count = pipelines.length
    if (!res.body.note) res.body.note = 'Use pipeline_id with aztea_run_pipeline to execute one of these workflows.'
  }
  return res
}

async function hireAsync(args) {
  const agentId = String(args.agent_id || '').trim()
  if (!agentId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'agent_id is required.' } }
  const body = { agent_id: agentId, input_payload: args.input_payload || {} }
  if (args.callback_url) body.callback_url = String(args.callback_url)
  if (args.max_attempts != null) body.max_attempts = Number(args.max_attempts)
  if (args.budget_cents != null) body.budget_cents = Number(args.budget_cents)
  if (args.private_task != null) body.private_task = Boolean(args.private_task)
  const res = parseApiResponse(await postJson('/jobs', body))
  if (res.ok) {
    accumulate(res.body.caller_charge_cents ?? res.body.price_cents)
    if (!res.body.note) res.body.note = `Job submitted. Poll with aztea_job_status(job_id='${res.body.job_id || ''}').`
  }
  return res
}

async function jobStatus(args) {
  const jobId = String(args.job_id || '').trim()
  if (!jobId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'job_id is required.' } }
  const job = parseApiResponse(await getJson(`/jobs/${jobId}`))
  if (!job.ok) return job
  let sinceQ = ''
  if (args.since_message_id != null) sinceQ = `?since=${encodeURIComponent(String(args.since_message_id))}`
  const msgs = parseApiResponse(await getJson(`/jobs/${jobId}/messages${sinceQ}`))
  const result = {
    job_id: job.body.job_id,
    status: job.body.status,
    agent_id: job.body.agent_id,
    created_at: job.body.created_at,
    updated_at: job.body.updated_at,
    price_cents: job.body.price_cents,
    output_payload: job.body.output_payload,
    error_message: job.body.error_message,
    output_verification_status: job.body.output_verification_status,
    output_verification_deadline_at: job.body.output_verification_deadline_at,
    messages: msgs.ok ? (msgs.body.messages || []) : [],
  }
  const clarifications = result.messages.filter(m => m && m.type === 'clarification_request')
  if (clarifications.length) {
    result.clarification_needed = clarifications[clarifications.length - 1].payload || {}
    result.note = 'Agent is awaiting clarification. Call aztea_clarify(job_id=..., message=...).'
  }
  return { ok: true, body: result }
}

async function clarify(args) {
  const jobId = String(args.job_id || '').trim()
  const message = String(args.message || '').trim()
  if (!jobId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'job_id is required.' } }
  if (!message) return { ok: false, body: { error: 'INVALID_INPUT', message: 'message is required.' } }
  let requestMessageId = args.request_message_id
  if (requestMessageId == null) {
    const msgs = parseApiResponse(await getJson(`/jobs/${jobId}/messages`))
    if (!msgs.ok) return { ok: false, body: { error: 'CLARIFICATION_LOOKUP_FAILED', message: 'Could not retrieve clarification requests.', ...msgs.body } }
    const latest = (msgs.body.messages || []).slice().reverse().find(m => m && m.type === 'clarification_request' && m.message_id)
    if (!latest) return { ok: false, body: { error: 'INVALID_INPUT', message: 'No clarification_request message found for this job.' } }
    requestMessageId = latest.message_id
  }
  const res = parseApiResponse(await postJson(`/jobs/${jobId}/messages`, {
    type: 'clarification_response',
    payload: { answer: message, request_message_id: Number(requestMessageId) },
  }))
  if (res.ok && !res.body.note) res.body.note = 'Clarification sent. The agent will resume shortly.'
  return res
}

async function rateJob(args) {
  const jobId = String(args.job_id || '').trim()
  const rating = Number(args.rating || 0)
  if (!jobId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'job_id is required.' } }
  if (!(rating >= 1 && rating <= 5)) return { ok: false, body: { error: 'INVALID_INPUT', message: 'rating must be 1-5.' } }
  return parseApiResponse(await postJson(`/jobs/${jobId}/rating`, { rating }))
}

async function disputeJob(args) {
  const jobId = String(args.job_id || '').trim()
  const reason = String(args.reason || '').trim()
  if (!jobId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'job_id is required.' } }
  if (!reason) return { ok: false, body: { error: 'INVALID_INPUT', message: 'reason is required.' } }
  const body = { reason }
  if (args.evidence) body.evidence = String(args.evidence)
  const res = parseApiResponse(await postJson(`/jobs/${jobId}/dispute`, body))
  if (res.ok && !res.body.note) res.body.note = 'Dispute filed. An LLM judge will review the evidence.'
  return res
}

async function verifyOutput(args) {
  const jobId = String(args.job_id || '').trim()
  const decision = String(args.decision || '').trim().toLowerCase()
  if (!jobId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'job_id is required.' } }
  if (!['accept', 'reject'].includes(decision)) return { ok: false, body: { error: 'INVALID_INPUT', message: "decision must be 'accept' or 'reject'." } }
  const body = { decision }
  if (args.reason) body.reason = String(args.reason)
  else if (decision === 'reject') return { ok: false, body: { error: 'INVALID_INPUT', message: "reason is required when decision is 'reject'." } }
  return parseApiResponse(await postJson(`/jobs/${jobId}/verification`, body))
}

async function discover(args) {
  const query = String(args.query || '').trim()
  if (!query) return { ok: false, body: { error: 'INVALID_INPUT', message: 'query is required.' } }
  const body = { query, limit: args.limit != null ? Math.max(1, Math.min(Number(args.limit), 20)) : 5 }
  if (args.min_trust_score != null) body.min_trust = Number(args.min_trust_score) / 100
  if (args.max_price_cents != null) body.max_price_cents = Number(args.max_price_cents)
  const res = parseApiResponse(await postJson('/registry/search', body))
  if (res.ok && Array.isArray(res.body.results)) {
    res.body.results = res.body.results.map(item => {
      const agent = item.agent || {}
      return {
        agent_id: agent.agent_id,
        name: agent.name,
        description: String(agent.description || '').slice(0, 200),
        price_per_call_usd: agent.price_per_call_usd,
        trust_score: agent.trust_score,
        success_rate: agent.success_rate,
        blended_score: item.blended_score,
        match_reasons: item.match_reasons,
      }
    })
  }
  return res
}

async function getExamples(args) {
  const agentId = String(args.agent_id || '').trim()
  if (!agentId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'agent_id is required.' } }
  const res = parseApiResponse(await getJson(`/registry/agents/${agentId}`))
  if (!res.ok) return res
  const examples = Array.isArray(res.body.output_examples) ? res.body.output_examples : []
  return {
    ok: true,
    body: {
      agent_id: agentId,
      name: res.body.name,
      example_count: examples.length,
      examples: examples.slice(0, 10),
      note: examples.length
        ? 'These are real past work examples. Review them before hiring.'
        : 'No public work examples are available for this agent yet.',
    },
  }
}

async function hireBatch(args) {
  const jobs = args.jobs
  if (!Array.isArray(jobs) || !jobs.length) return { ok: false, body: { error: 'INVALID_INPUT', message: 'jobs must be a non-empty array.' } }
  if (jobs.length > 50) return { ok: false, body: { error: 'INVALID_INPUT', message: 'Batch size is limited to 50 jobs.' } }
  const body = {
    jobs: jobs.map(spec => ({
      agent_id: String(spec.agent_id || ''),
      input_payload: spec.input_payload || {},
      ...(spec.budget_cents != null ? { budget_cents: Number(spec.budget_cents) } : {}),
      ...(spec.private_task != null ? { private_task: Boolean(spec.private_task) } : {}),
    })),
  }
  const res = parseApiResponse(await postJson('/jobs/batch', body))
  if (res.ok) {
    accumulate(res.body.total_price_cents)
    if (!res.body.job_ids) res.body.job_ids = (res.body.jobs || []).map(j => j && j.job_id).filter(Boolean)
    if (!res.body.note) res.body.note = `Batch of ${jobs.length} jobs submitted. Poll each job_id with aztea_job_status.`
  }
  return res
}

async function compareStatus(compareId) {
  const res = parseApiResponse(await getJson(`/jobs/compare/${compareId}`))
  if (res.ok) {
    const status = String(res.body.status || '').toLowerCase()
    if (status === 'complete' && !res.body.note) res.body.note = 'Compare session completed. Call aztea_select_compare_winner to finalize payment.'
    if (status === 'running' && !res.body.note) res.body.note = 'Compare session is still running.'
  }
  return res
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function compareAgents(args) {
  const agentIds = Array.isArray(args.agent_ids) ? args.agent_ids.map(x => String(x || '').trim()).filter(Boolean) : null
  if (!agentIds) return { ok: false, body: { error: 'INVALID_INPUT', message: 'agent_ids must be an array.' } }
  if (agentIds.length < 2 || agentIds.length > 3) return { ok: false, body: { error: 'INVALID_INPUT', message: 'agent_ids must contain 2 or 3 values.' } }
  if (!args.input_payload || typeof args.input_payload !== 'object' || Array.isArray(args.input_payload)) {
    return { ok: false, body: { error: 'INVALID_INPUT', message: 'input_payload must be an object.' } }
  }
  const body = { agent_ids: agentIds, input_payload: args.input_payload }
  if (args.max_attempts != null) body.max_attempts = Number(args.max_attempts)
  if (args.private_task != null) body.private_task = Boolean(args.private_task)
  const created = parseApiResponse(await postJson('/jobs/compare', body))
  if (!created.ok) return created
  accumulate(created.body.total_charged_cents)
  const compareId = String(created.body.compare_id || '').trim()
  if (!compareId) return created
  const waitSeconds = Math.max(1, Math.min(Number(args.wait_seconds || 30), 300))
  const pollMs = Math.max(500, Math.min(Number(args.poll_interval_seconds || 2) * 1000, 10000))
  const deadline = Date.now() + waitSeconds * 1000
  let latest = created.body
  while (Date.now() < deadline) {
    const status = await compareStatus(compareId)
    if (!status.ok) return status
    latest = status.body
    if (String(latest.status || '').toLowerCase() === 'complete') {
      if (created.body.total_charged_cents != null && latest.total_charged_cents == null) latest.total_charged_cents = created.body.total_charged_cents
      return { ok: true, body: latest }
    }
    await sleep(pollMs)
  }
  if (!latest.note) latest.note = 'Compare session is still running. Poll it with aztea_compare_status.'
  if (created.body.total_charged_cents != null && latest.total_charged_cents == null) latest.total_charged_cents = created.body.total_charged_cents
  return { ok: true, body: latest }
}

async function selectCompareWinner(args) {
  const compareId = String(args.compare_id || '').trim()
  const winnerAgentId = String(args.winner_agent_id || '').trim()
  if (!compareId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'compare_id is required.' } }
  if (!winnerAgentId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'winner_agent_id is required.' } }
  const res = parseApiResponse(await postJson(`/jobs/compare/${compareId}/select`, { winner_agent_id: winnerAgentId }))
  if (res.ok && !res.body.note) res.body.note = 'Compare session finalized. Only the winner was paid.'
  return res
}

async function pipelineStatus(args) {
  const pipelineId = String(args.pipeline_id || '').trim()
  const runId = String(args.run_id || '').trim()
  if (!pipelineId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'pipeline_id is required.' } }
  if (!runId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'run_id is required.' } }
  const res = parseApiResponse(await getJson(`/pipelines/${pipelineId}/runs/${runId}`))
  if (res.ok) {
    const status = String(res.body.status || '').toLowerCase()
    if (status === 'complete' && !res.body.note) res.body.note = 'Pipeline run completed.'
    if (status === 'failed' && !res.body.note) res.body.note = 'Pipeline run failed. Inspect error_message and step_results.'
    if (status === 'running' && !res.body.note) res.body.note = 'Pipeline run is still running.'
  }
  return res
}

async function pollPipelineRun(pipelineId, runId, waitSeconds, pollSeconds) {
  const deadline = Date.now() + waitSeconds * 1000
  let latest = { run_id: runId, pipeline_id: pipelineId, status: 'running' }
  while (Date.now() < deadline) {
    const res = await pipelineStatus({ pipeline_id: pipelineId, run_id: runId })
    if (!res.ok) return res
    latest = res.body
    const status = String(latest.status || '').toLowerCase()
    if (['complete', 'failed', 'cancelled'].includes(status)) return { ok: true, body: latest }
    await sleep(Math.max(500, Math.min(Number(pollSeconds) * 1000, 10000)))
  }
  if (!latest.note) latest.note = 'Pipeline run is still running. Poll it with aztea_pipeline_status.'
  return { ok: true, body: latest }
}

async function runPipeline(args) {
  const pipelineId = String(args.pipeline_id || '').trim()
  if (!pipelineId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'pipeline_id is required.' } }
  if (!args.input_payload || typeof args.input_payload !== 'object' || Array.isArray(args.input_payload)) {
    return { ok: false, body: { error: 'INVALID_INPUT', message: 'input_payload must be an object.' } }
  }
  const created = parseApiResponse(await postJson(`/pipelines/${pipelineId}/run`, { input_payload: args.input_payload }))
  if (!created.ok) return created
  const runId = String(created.body.run_id || '').trim()
  if (!runId) return created
  return pollPipelineRun(pipelineId, runId, Math.max(1, Math.min(Number(args.wait_seconds || 30), 300)), Number(args.poll_interval_seconds || 2))
}

async function runRecipe(args) {
  const recipeId = String(args.recipe_id || args.recipe_name || '').trim()
  if (!recipeId) return { ok: false, body: { error: 'INVALID_INPUT', message: 'recipe_id or recipe_name is required.' } }
  if (!args.input_payload || typeof args.input_payload !== 'object' || Array.isArray(args.input_payload)) {
    return { ok: false, body: { error: 'INVALID_INPUT', message: 'input_payload must be an object.' } }
  }
  const created = parseApiResponse(await postJson(`/recipes/${recipeId}/run`, { input_payload: args.input_payload }))
  if (!created.ok) return created
  const pipelineId = String(created.body.pipeline_id || recipeId).trim()
  const runId = String(created.body.run_id || '').trim()
  if (!runId) return created
  const status = await pollPipelineRun(pipelineId, runId, Math.max(1, Math.min(Number(args.wait_seconds || 30), 300)), Number(args.poll_interval_seconds || 2))
  if (status.ok) status.body.recipe_id = recipeId
  return status
}

async function callMetaTool(name, args) {
  if (name === 'aztea_set_session_budget') {
    const budget = Number(args.budget_cents || 0)
    SESSION_STATE.budgetCents = budget > 0 ? budget : null
    return {
      ok: true,
      body: {
        budget_cents: SESSION_STATE.budgetCents,
        spent_cents: SESSION_STATE.spentCents,
        message: budget > 0
          ? `Session budget set to $${(budget / 100).toFixed(2)}. Current session spend: $${(SESSION_STATE.spentCents / 100).toFixed(2)}.`
          : 'Session budget cleared.',
      },
    }
  }

  const blocked = budgetGuard()
  if (blocked) return { ok: false, body: blocked }

  switch (name) {
    case 'aztea_wallet_balance': return walletBalance()
    case 'aztea_spend_summary': return spendSummary(args)
    case 'aztea_set_daily_limit': return setDailyLimit(args)
    case 'aztea_topup_url': return topupUrl(args)
    case 'aztea_session_summary': return sessionSummary()
    case 'aztea_estimate_cost': return estimateCost(args)
    case 'aztea_list_recipes': return listRecipes()
    case 'aztea_list_pipelines': return listPipelines()
    case 'aztea_hire_async': return hireAsync(args)
    case 'aztea_job_status': return jobStatus(args)
    case 'aztea_clarify': return clarify(args)
    case 'aztea_rate_job': return rateJob(args)
    case 'aztea_dispute_job': return disputeJob(args)
    case 'aztea_verify_output': return verifyOutput(args)
    case 'aztea_discover': return discover(args)
    case 'aztea_get_examples': return getExamples(args)
    case 'aztea_hire_batch': return hireBatch(args)
    case 'aztea_compare_agents': return compareAgents(args)
    case 'aztea_compare_status': return compareStatus(String(args.compare_id || '').trim())
    case 'aztea_select_compare_winner': return selectCompareWinner(args)
    case 'aztea_run_pipeline': return runPipeline(args)
    case 'aztea_pipeline_status': return pipelineStatus(args)
    case 'aztea_run_recipe': return runRecipe(args)
    default: return { ok: false, body: { error: 'UNKNOWN_META_TOOL', tool: name } }
  }
}

async function callRegistryTool(entry, args) {
  const res = await postJson(`/registry/agents/${entry.agent_id}/call`, args || {})
  if (res.status === 401 || res.status === 403) {
    _authRequired = true
    return { ok: false, body: authRequiredResponse() }
  }
  return parseApiResponse(res)
}

function parseDataUri(value) {
  const text = String(value || '').trim()
  const match = /^data:([^;,]+);base64,([A-Za-z0-9+/=]+)$/i.exec(text)
  return match ? { mime: match[1].trim().toLowerCase(), data: match[2].trim() } : null
}

function contentFromPayload(payload) {
  const content = [{
    type: 'text',
    text: typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2),
  }]
  if (payload && typeof payload === 'object' && Array.isArray(payload.artifacts)) {
    for (const artifact of payload.artifacts.slice(0, 6)) {
      const source = String(artifact && artifact.url_or_base64 || '').trim()
      const parsed = parseDataUri(source)
      const mime = String(artifact && artifact.mime || '').trim().toLowerCase() || (parsed && parsed.mime) || ''
      if (parsed && mime.startsWith('image/')) {
        content.push({ type: 'image', mimeType: mime, data: parsed.data })
      }
    }
  }
  return content
}

async function callTool(name, args) {
  if (_authRequired || !API_KEY || name === AUTH_TOOL.name) return { ok: false, payload: authRequiredResponse() }
  if (name === LAZY_SEARCH_TOOL.name) {
    const query = String(args.query || '').trim()
    if (!query) return { ok: false, payload: { error: 'INVALID_INPUT', message: 'query is required.' } }
    if (!_catalog.length) await refreshCatalog()
    return { ok: true, payload: searchCatalog(query, args.limit) }
  }
  if (name === LAZY_DESCRIBE_TOOL.name) {
    const slug = String(args.slug || '').trim()
    if (!slug) return { ok: false, payload: { error: 'INVALID_INPUT', message: 'slug is required.' } }
    if (!_catalog.length) await refreshCatalog()
    return describeCatalog(slug)
  }
  if (name === LAZY_CALL_TOOL.name) {
    const slug = String(args.slug || '').trim()
    if (!slug) return { ok: false, payload: { error: 'INVALID_INPUT', message: 'slug is required.' } }
    if (LAZY_TOOL_NAMES.has(slug)) return { ok: false, payload: { error: 'INVALID_INPUT', message: 'Use the lazy MCP tools directly, not via aztea_call.' } }
    if (args.arguments == null || typeof args.arguments !== 'object' || Array.isArray(args.arguments)) {
      return { ok: false, payload: { error: 'INVALID_INPUT', message: 'arguments must be an object.' } }
    }
    if (!_catalog.length) await refreshCatalog()
    const entry = _catalog.find(item => item.slug === slug)
    if (!entry) return { ok: false, payload: { error: 'TOOL_NOT_FOUND', message: `Unknown tool '${slug}'.`, hint: 'Use aztea_search first.' } }
    if (META_TOOL_NAMES.has(entry.slug)) {
      const res = await callMetaTool(entry.slug, args.arguments)
      return { ok: res.ok, payload: res.body }
    }
    if (!entry.agent_id) return { ok: false, payload: { error: 'TOOL_NOT_FOUND', message: `Tool '${slug}' has no agent_id.` } }
    const res = await callRegistryTool(entry, args.arguments)
    return { ok: res.ok, payload: res.body }
  }
  return { ok: false, payload: { error: 'TOOL_NOT_FOUND', message: `Unknown tool: ${name}` } }
}

function readMessages() {
  let buf = ''
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', chunk => {
    buf += chunk
    let nl
    while ((nl = buf.indexOf('\n')) !== -1) {
      const line = buf.slice(0, nl).replace(/\r$/, '')
      buf = buf.slice(nl + 1)
      if (!line.trim()) continue
      let msg
      try { msg = JSON.parse(line) } catch { continue }
      handleMessage(msg).catch(err => log(`request failed: ${err.message}`))
    }
  })
  process.stdin.on('end', () => process.exit(0))
}

async function handleMessage(msg) {
  if (!msg || typeof msg !== 'object' || !('id' in msg)) return
  const { id, method, params } = msg
  const reply = result => writeMsg({ jsonrpc: '2.0', id, result })
  const error = (code, message) => writeMsg({ jsonrpc: '2.0', id, error: { code, message } })

  if (method === 'initialize') {
    return reply({
      protocolVersion: '2024-11-05',
      capabilities: { tools: { listChanged: true } },
      serverInfo: { name: 'aztea-registry-mcp', version: '0.4.0' },
      instructions: SERVER_INSTRUCTIONS,
    })
  }
  if (method === 'ping') return reply({})
  if (method === 'tools/list') return reply({ tools: getTools() })
  if (method === 'tools/call') {
    if (!params || typeof params !== 'object') return error(-32602, 'params required')
    const name = String(params.name || '').trim()
    const args = params.arguments && typeof params.arguments === 'object' && !Array.isArray(params.arguments) ? params.arguments : {}
    if (!name) return error(-32602, 'name required')
    const result = await callTool(name, args)
    return reply({
      content: contentFromPayload(result.payload),
      structuredContent: result.payload && typeof result.payload === 'object' ? result.payload : { result: result.payload },
      ...(result.ok ? {} : { isError: true }),
    })
  }
  return error(-32601, `Method '${method}' not found`)
}

function run() {
  if (!API_KEY) log('No AZTEA_API_KEY set — run `npx aztea-cli init` to configure.')
  readMessages()
  refreshCatalog().catch(err => log(`initial refresh failed: ${err.message}`))
  setInterval(() => { refreshCatalog().catch(err => log(`refresh failed: ${err.message}`)) }, REFRESH_MS)
}

module.exports = { run }
if (require.main === module) run()
