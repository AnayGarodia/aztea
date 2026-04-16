const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').trim()
const BASE = (RAW_BASE || '/api').replace(/\/+$/, '')
const VERSION = '1.0'

function requestHeaders(key, { idempotencyKey } = {}) {
  const out = {
    'Content-Type': 'application/json',
    'X-AgentMarket-Version': VERSION,
  }
  if (key) out.Authorization = `Bearer ${key}`
  if (idempotencyKey) out['Idempotency-Key'] = idempotencyKey
  return out
}

function detailToString(detail) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail == null) return null
  if (typeof detail === 'number' || typeof detail === 'boolean') return String(detail)
  if (Array.isArray(detail)) {
    const joined = detail.map(item => detailToString(item) ?? '').filter(Boolean).join(', ')
    return joined || null
  }
  if (typeof detail === 'object') {
    if (typeof detail.error === 'string' && detail.error) return detail.error
    return JSON.stringify(detail)
  }
  return null
}

async function parseResponseBody(response) {
  const contentType = (response.headers.get('content-type') || '').toLowerCase()
  if (contentType.includes('application/json')) {
    return response.json().catch(() => null)
  }
  const text = await response.text().catch(() => '')
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function makeApiError(response, parsedBody) {
  const detail = parsedBody && typeof parsedBody === 'object' ? parsedBody.detail : null
  const message =
    detailToString(detail) ??
    (typeof parsedBody === 'string' && parsedBody.trim() ? parsedBody : null) ??
    `HTTP ${response.status}`
  const err = new Error(message)
  err.status = response.status
  err.body = parsedBody
  return err
}

async function request(path, {
  method = 'GET',
  key,
  body,
  idempotencyKey,
  throwOnError = true,
} = {}) {
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers: requestHeaders(key, { idempotencyKey }),
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const parsedBody = await parseResponseBody(response)
  if (!response.ok && throwOnError) throw makeApiError(response, parsedBody)
  return {
    ok: response.ok,
    status: response.status,
    body: parsedBody,
    headers: response.headers,
  }
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export async function authRegister(username, email, password) {
  const { body } = await request('/auth/register', {
    method: 'POST',
    body: { username, email, password },
  })
  return body
}

export async function authLogin(email, password) {
  const { body } = await request('/auth/login', {
    method: 'POST',
    body: { email, password },
  })
  return body
}

export async function authMe(key) {
  const { body } = await request('/auth/me', { key })
  return body
}

export async function fetchAuthKeys(key) {
  const { body } = await request('/auth/keys', { key })
  return body
}

export async function createAuthKey(key, name, scopes = ['caller', 'worker']) {
  const { body } = await request('/auth/keys', {
    method: 'POST',
    key,
    body: { name, scopes },
  })
  return body
}

export async function rotateAuthKey(key, keyId, payload = {}) {
  const { body } = await request(`/auth/keys/${keyId}/rotate`, {
    method: 'POST',
    key,
    body: payload,
  })
  return body
}

export async function deleteAuthKey(key, keyId) {
  const { body } = await request(`/auth/keys/${keyId}`, {
    method: 'DELETE',
    key,
  })
  return body
}

// ── Health ────────────────────────────────────────────────────────────────────

export async function fetchHealth(key) {
  const { body } = await request('/health', { key })
  return body
}

// ── Registry ──────────────────────────────────────────────────────────────────

export async function fetchAgents(key, tag, { rankBy = 'trust' } = {}) {
  const params = new URLSearchParams()
  if (tag) params.set('tag', tag)
  if (rankBy) params.set('rank_by', rankBy)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const { body } = await request(`/registry/agents${suffix}`, { key })
  return body
}

export async function registerAgent(key, data) {
  const { body } = await request('/registry/register', {
    method: 'POST',
    key,
    body: data,
  })
  return body
}

export async function searchAgents(key, query) {
  const trimmed = String(query ?? '').trim()
  const { body } = await request('/registry/search', {
    method: 'POST',
    key,
    body: { query: trimmed },
  })
  return body
}

// ── Calls (sync) ──────────────────────────────────────────────────────────────

export async function callAgent(key, agentId, payload) {
  const result = await request(`/registry/agents/${agentId}/call`, {
    method: 'POST',
    key,
    body: payload,
    throwOnError: false,
  })
  return { status: result.status, ok: result.ok, body: result.body }
}

// ── Jobs (async) ──────────────────────────────────────────────────────────────

export async function createJob(key, agentId, inputPayload, maxAttempts = 3) {
  const { body } = await request('/jobs', {
    method: 'POST',
    key,
    body: { agent_id: agentId, input_payload: inputPayload, max_attempts: maxAttempts },
  })
  return body
}

export async function fetchJobs(key, { limit = 50, status, cursor } = {}) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (status) params.set('status', status)
  if (cursor) params.set('cursor', cursor)
  const { body } = await request(`/jobs?${params.toString()}`, { key })
  return body
}

export async function fetchAgentJobs(key, agentId, { limit = 50, status, cursor } = {}) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (status) params.set('status', status)
  if (cursor) params.set('cursor', cursor)
  const { body } = await request(`/jobs/agent/${agentId}?${params.toString()}`, { key })
  return body
}

export async function fetchAllAgentJobs(key, agentId, { status, pageSize = 100, maxPages = 5 } = {}) {
  let cursor = null
  const jobs = []
  for (let page = 0; page < maxPages; page += 1) {
    const data = await fetchAgentJobs(key, agentId, { limit: pageSize, status, cursor })
    jobs.push(...(data.jobs ?? []))
    cursor = data.next_cursor || null
    if (!cursor) break
  }
  return { jobs, next_cursor: cursor }
}

export async function fetchAllJobs(key, { status, pageSize = 100, maxPages = 5 } = {}) {
  let cursor = null
  const jobs = []
  for (let page = 0; page < maxPages; page += 1) {
    const data = await fetchJobs(key, { limit: pageSize, status, cursor })
    jobs.push(...(data.jobs ?? []))
    cursor = data.next_cursor || null
    if (!cursor) break
  }
  return { jobs, next_cursor: cursor }
}

export async function getJob(key, jobId) {
  const { body } = await request(`/jobs/${jobId}`, { key })
  return body
}

export async function claimJob(key, jobId, leaseSeconds = 300) {
  const { body } = await request(`/jobs/${jobId}/claim`, {
    method: 'POST',
    key,
    body: { lease_seconds: leaseSeconds },
  })
  return body
}

export async function heartbeatJob(key, jobId, leaseSeconds = 300, claimToken) {
  const { body } = await request(`/jobs/${jobId}/heartbeat`, {
    method: 'POST',
    key,
    body: { lease_seconds: leaseSeconds, claim_token: claimToken || null },
  })
  return body
}

export async function completeJob(key, jobId, outputPayload, { claimToken, idempotencyKey } = {}) {
  const { body } = await request(`/jobs/${jobId}/complete`, {
    method: 'POST',
    key,
    body: { output_payload: outputPayload, claim_token: claimToken || null },
    idempotencyKey,
  })
  return body
}

export async function failJob(key, jobId, errorMessage, { claimToken, idempotencyKey } = {}) {
  const { body } = await request(`/jobs/${jobId}/fail`, {
    method: 'POST',
    key,
    body: { error_message: errorMessage || null, claim_token: claimToken || null },
    idempotencyKey,
  })
  return body
}

export async function getJobMessages(key, jobId, sinceId) {
  const suffix = sinceId != null ? `?since=${encodeURIComponent(String(sinceId))}` : ''
  const { body } = await request(`/jobs/${jobId}/messages${suffix}`, { key })
  return body
}

export async function postJobMessage(key, jobId, payload) {
  const { body } = await request(`/jobs/${jobId}/messages`, {
    method: 'POST',
    key,
    body: payload,
  })
  return body
}

export async function rateJob(key, jobId, rating, { idempotencyKey } = {}) {
  const { body } = await request(`/jobs/${jobId}/rating`, {
    method: 'POST',
    key,
    body: { rating },
    idempotencyKey,
  })
  return body
}

// ── Disputes ──────────────────────────────────────────────────────────────────

export async function getJobDispute(key, jobId) {
  // Returns the dispute for this job, or null if none exists
  const { body, status } = await request(`/jobs/${jobId}/dispute`, { key })
  if (status === 404) return null
  return body
}

export async function fileDispute(key, jobId, { reason, evidence, side }) {
  const { body } = await request(`/jobs/${jobId}/dispute`, {
    method: 'POST',
    key,
    body: { reason, evidence, side },
  })
  return body
}

export async function getDispute(key, disputeId) {
  const { body } = await request(`/ops/disputes/${disputeId}`, { key })
  return body
}

export async function registerHook(key, targetUrl, secret = null) {
  const { body } = await request('/ops/jobs/hooks', {
    method: 'POST',
    key,
    body: { target_url: targetUrl, secret },
  })
  return body
}

// ── Wallet ────────────────────────────────────────────────────────────────────

export async function fetchWalletMe(key) {
  const { body } = await request('/wallets/me', { key })
  return body
}

export async function depositToWallet(key, walletId, amountCents, memo = 'dashboard deposit') {
  const { body } = await request('/wallets/deposit', {
    method: 'POST',
    key,
    body: { wallet_id: walletId, amount_cents: amountCents, memo },
  })
  return body
}

export async function createTopupSession(key, walletId, amountCents) {
  const { body } = await request('/wallets/topup/session', {
    method: 'POST',
    key,
    body: { wallet_id: walletId, amount_cents: amountCents },
  })
  return body // { checkout_url, session_id }
}

export async function fetchAgentEarnings(key) {
  const { body } = await request('/wallets/me/agent-earnings', { key })
  return body // { earnings: [{ agent_id, agent_name, total_earned_cents, call_count, last_earned_at }] }
}

export async function fetchPublicConfig() {
  const { body } = await request('/config/public', {})
  return body // { stripe_enabled, stripe_publishable_key }
}

// ── Runs ──────────────────────────────────────────────────────────────────────

export async function fetchRuns(key, limit = 50) {
  const { body } = await request(`/runs?limit=${encodeURIComponent(String(limit))}`, { key })
  return body
}
