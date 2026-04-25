const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').trim()
const BASE = (RAW_BASE || '/api').replace(/\/+$/, '')
const VERSION = '1.0'

let _onSessionExpired = null
export function setSessionExpiredHandler(fn) { _onSessionExpired = fn }

function makeRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 12)}`
}

function requestHeaders(key, { idempotencyKey, requestId } = {}) {
  const out = {
    'Content-Type': 'application/json',
    'X-Aztea-Version': VERSION,
  }
  if (key) out.Authorization = `Bearer ${key}`
  if (idempotencyKey) out['Idempotency-Key'] = idempotencyKey
  if (requestId) out['X-Request-ID'] = String(requestId)
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

const HTTP_STATUS_MESSAGES = {
  400: 'Bad request. Check your input and try again.',
  401: 'Not authenticated. Please sign in.',
  403: "You don't have permission to do that.",
  404: 'Not found.',
  409: 'Conflict: this already exists.',
  422: 'Invalid input. Check the fields and try again.',
  429: 'Too many requests. Wait a moment and try again.',
  500: 'Server error. Try again in a moment.',
  502: 'Payment processor error. Try again.',
  503: 'Service temporarily unavailable.',
}

// Friendly fallbacks used ONLY when the server returns a generic/missing message.
// Specific server messages (e.g. pydantic validation errors) always take priority.
const API_ERROR_MESSAGE_BY_CODE = {
  'registry.url_forbidden': 'That URL is blocked for safety. Use a public HTTPS endpoint.',
  'registry.url_invalid': 'Endpoint URL is invalid. Use a public HTTPS URL.',
  'registry.agent_limit': 'You reached your agent limit. Delete/archive an existing agent or use another account.',
  'payment.stripe_insufficient_funds': 'Payouts are temporarily unavailable because platform Stripe balance is low. Please retry later.',
  'payment.stripe_destination_invalid': 'Your payout account is unavailable. Reconnect your bank account and try again.',
  'payment.stripe_connect_unavailable': 'Stripe Connect is not enabled on this server yet. Please contact support.',
  'payment.stripe_rate_limited': 'Stripe is rate-limiting requests right now. Please retry in a moment.',
  'payment.stripe_auth_error': 'Payments are temporarily unavailable due to Stripe configuration.',
  'payment.stripe_upstream_error': 'Stripe is temporarily unavailable. Please try again.',
  'payment.stripe_error': 'Payment processor error. Please try again.',
  'payment.topup_daily_limit_exceeded': 'Daily top-up limit reached. Try a smaller amount or wait until the rolling 24h window resets.',
}

// Server messages that are too generic to show to users as-is.
const GENERIC_SERVER_MESSAGES = new Set([
  'not found',
  'request failed.',
  'internal server error.',
  'bad request',
  'forbidden',
  'unauthorized',
])

function isGenericMessage(message) {
  if (!message) return true
  const lowered = String(message).trim().toLowerCase()
  return GENERIC_SERVER_MESSAGES.has(lowered)
}

function makeApiError(response, parsedBody) {
  let message = null
  let errorCode = null

  if (parsedBody && typeof parsedBody === 'object') {
    if (typeof parsedBody.error === 'string' && parsedBody.error.trim()) {
      errorCode = parsedBody.error.trim()
    }
    // Prefer server-provided structured message/sub-errors so users see the
    // most specific, actionable guidance possible.
    if (typeof parsedBody.message === 'string' && parsedBody.message.trim()) {
      message = parsedBody.message.trim()
      const errors = parsedBody.data?.errors ?? parsedBody.details?.errors
      if (Array.isArray(errors) && errors.length > 0) {
        const sub = errors[0]?.msg ?? errors[0]?.message ?? null
        if (sub && typeof sub === 'string') {
          message = sub.replace(/^Value error,\s*/i, '')
        }
      }
    }
    if (!message || isGenericMessage(message)) {
      const fromDetail = detailToString(parsedBody.detail)
      if (fromDetail && !isGenericMessage(fromDetail)) message = fromDetail
    }
  }

  if ((!message || isGenericMessage(message)) && errorCode && API_ERROR_MESSAGE_BY_CODE[errorCode]) {
    message = API_ERROR_MESSAGE_BY_CODE[errorCode]
  }

  if (!message && typeof parsedBody === 'string' && parsedBody.trim()) message = parsedBody.trim()

  if ((!message || isGenericMessage(message)) && response.status === 404) {
    const pathname = pathnameFromResponse(response)
    if (pathname.startsWith('/api/')) {
      message = 'API route not found. If you are self-hosting, ensure /api/* is proxied to the backend (or the /api prefix shim is active).'
    }
  }
  if (!message) message = HTTP_STATUS_MESSAGES[response.status] ?? `Unexpected error (HTTP ${response.status})`

  const err = new Error(message)
  err.status = response.status
  err.body = parsedBody
  err.code = errorCode || null
  if (parsedBody && typeof parsedBody === 'object' && typeof parsedBody.request_id === 'string' && parsedBody.request_id) {
    err.requestId = parsedBody.request_id
  } else {
    const hdrRid = response.headers?.get?.('X-Request-ID')
    if (hdrRid) err.requestId = String(hdrRid)
  }
  return err
}

function pathnameFromResponse(response) {
  try {
    return new URL(response.url).pathname || ''
  } catch {
    return ''
  }
}

const REQUEST_TIMEOUT_MS = 30_000

function timeoutSignal(ms) {
  if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function') {
    return AbortSignal.timeout(ms)
  }
  const controller = new AbortController()
  setTimeout(() => controller.abort(), ms)
  return controller.signal
}

async function request(path, {
  method = 'GET',
  key,
  body,
  idempotencyKey,
  requestId: explicitRequestId,
  throwOnError = true,
} = {}) {
  const requestId = explicitRequestId ?? makeRequestId()
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: requestHeaders(key, { idempotencyKey, requestId }),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: timeoutSignal(REQUEST_TIMEOUT_MS),
    })
  } catch (err) {
    if (err?.name === 'AbortError' || err?.name === 'TimeoutError') {
      const timeoutErr = new Error('Request timed out. The server may be unreachable. Please try again.')
      timeoutErr.status = 0
      timeoutErr.code = 'network.timeout'
      throw timeoutErr
    }
    const netErr = new Error(err?.message || 'Network error. Check your connection and try again.')
    netErr.status = 0
    netErr.code = 'network.error'
    throw netErr
  }
  const parsedBody = await parseResponseBody(response)
  if (!response.ok && throwOnError) {
    if (response.status === 401 && _onSessionExpired && !path.startsWith('/auth/')) _onSessionExpired()
    throw makeApiError(response, parsedBody)
  }
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

export async function authForgotPassword(email) {
  const { body } = await request('/auth/forgot-password', {
    method: 'POST',
    body: { email },
  })
  return body
}

export async function authResetPassword(email, otp, new_password) {
  const { body } = await request('/auth/reset-password', {
    method: 'POST',
    body: { email, otp, new_password },
  })
  return body
}

export async function authAcceptLegal(key, termsVersion, privacyVersion) {
  const { body } = await request('/auth/legal/accept', {
    method: 'POST',
    key,
    body: {
      terms_version: termsVersion,
      privacy_version: privacyVersion,
    },
  })
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

export async function fetchPublicDocsIndex() {
  const { body } = await request('/public/docs')
  return body
}

export async function fetchPublicDoc(slug) {
  const { body } = await request(`/public/docs/${encodeURIComponent(String(slug ?? '').trim())}`)
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

export async function fetchMyAgents(key) {
  const { body } = await request('/registry/agents/mine', { key })
  return body
}

export async function updateAgent(key, agentId, data) {
  const { body } = await request(`/registry/agents/${agentId}`, {
    method: 'PATCH',
    key,
    body: data,
  })
  return body
}

export async function delistAgent(key, agentId) {
  const { body } = await request(`/registry/agents/${agentId}`, {
    method: 'DELETE',
    key,
  })
  return body
}

export async function searchAgents(key, query, { model_provider } = {}) {
  const trimmed = String(query ?? '').trim()
  const bodyData = { query: trimmed }
  if (model_provider) bodyData.model_provider = model_provider
  const { body } = await request('/registry/search', {
    method: 'POST',
    key,
    body: bodyData,
  })
  return body
}

export async function fetchMcpTools(key) {
  const { body } = await request('/mcp/tools', { key })
  return body // { tools: [...], count: N }
}

// ── Calls (sync) ──────────────────────────────────────────────────────────────

export async function callAgent(key, agentId, payload, { privateTask = false } = {}) {
  const body = privateTask ? { ...payload, private_task: true } : payload
  const result = await request(`/registry/agents/${agentId}/call`, {
    method: 'POST',
    key,
    body,
    throwOnError: false,
  })
  return { status: result.status, ok: result.ok, body: result.body }
}

export async function fetchAdminDisputes(key, { limit = 200, status } = {}) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (status) params.set('status', status)
  const { body } = await request(`/admin/disputes?${params}`, { key })
  return body
}

export async function fetchAdminDispute(key, disputeId) {
  const { body } = await request(`/admin/disputes/${disputeId}`, { key })
  return body
}

export async function ruleDispute(key, disputeId, { outcome, reasoning, split_caller_cents, split_agent_cents } = {}) {
  const payload = { outcome, reasoning }
  if (outcome === 'split') {
    payload.split_caller_cents = split_caller_cents
    payload.split_agent_cents = split_agent_cents
  }
  const { body } = await request(`/admin/disputes/${disputeId}/rule`, {
    method: 'POST',
    key,
    body: payload,
  })
  return body
}

export async function fetchAdminPlatformEarnings(key) {
  const { body } = await request('/admin/platform/earnings', { key })
  return body
}

export async function adminPlatformWithdraw(key, { source, amount_cents, memo } = {}) {
  const { body } = await request('/admin/platform/withdraw', {
    method: 'POST',
    key,
    body: { source, amount_cents, ...(memo ? { memo } : {}) },
  })
  return body
}

export async function verifyJob(key, jobId, { decision, reason } = {}) {
  const { body } = await request(`/jobs/${jobId}/verification`, {
    method: 'POST',
    key,
    body: { decision, ...(reason ? { reason } : {}) },
  })
  return body
}

export async function fetchAgentWorkHistory(key, agentId, { limit = 20, offset = 0 } = {}) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  const { body } = await request(`/registry/agents/${agentId}/work-history?${params}`, { key })
  return body
}

export async function fetchLLMProviders(key) {
  const { body } = await request('/llm/providers', { key })
  return body
}

// ── Jobs (async) ──────────────────────────────────────────────────────────────

export async function createJob(key, agentId, inputPayload, maxAttempts = 3, { budgetCents, callbackUrl, privateTask } = {}) {
  const payload = { agent_id: agentId, input_payload: inputPayload, max_attempts: maxAttempts }
  if (budgetCents != null) payload.budget_cents = budgetCents
  if (callbackUrl) payload.callback_url = callbackUrl
  if (privateTask) payload.private_task = true
  const { body } = await request('/jobs', { method: 'POST', key, body: payload })
  return body
}

export async function createJobBatch(key, specs) {
  const { body } = await request('/jobs/batch', { method: 'POST', key, body: { jobs: specs } })
  return body
}

export async function fetchSpendSummary(key, period = '7d') {
  const { body } = await request(`/wallets/spend-summary?period=${period}`, { key })
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

export async function fetchWithdrawals(key, limit = 20) {
  const { body } = await request(`/wallets/withdrawals?limit=${encodeURIComponent(String(limit))}`, { key })
  return body // { withdrawals: [{ transfer_id, amount_cents, stripe_tx_id, memo, created_at, status }], count }
}

export async function fetchAgentEarnings(key) {
  const { body } = await request('/wallets/me/agent-earnings', { key })
  return body // { earnings: [{ agent_id, agent_name, total_earned_cents, call_count, last_earned_at, current_balance_cents, ... }] }
}

// ── Agent sub-wallets ─────────────────────────────────────────────────────────

export async function fetchAgentWallets(key) {
  const { body } = await request('/wallets/me/agents', { key })
  return body // { agents: [{ agent_id, agent_name, wallet_id, current_balance_cents, total_earned_cents, total_spent_cents, call_count, last_earned_at, guarantor_enabled, guarantor_cap_cents, daily_spend_limit_cents, display_label }] }
}

export async function fetchAgentWalletTransactions(key, agentId, limit = 50) {
  const { body } = await request(
    `/wallets/agents/${encodeURIComponent(agentId)}/transactions?limit=${encodeURIComponent(String(limit))}`,
    { key },
  )
  return body // { wallet_id, agent_id, transactions: [...] }
}

export async function updateAgentWalletSettings(key, agentId, body) {
  const { body: respBody } = await request(
    `/wallets/agents/${encodeURIComponent(agentId)}/settings`,
    { method: 'PATCH', key, body },
  )
  return respBody
}

export async function sweepAgentWallet(key, agentId, amountCents = null) {
  const { body } = await request(
    `/wallets/agents/${encodeURIComponent(agentId)}/sweep`,
    { method: 'POST', key, body: amountCents == null ? {} : { amount_cents: amountCents } },
  )
  return body // { agent_id, wallet_id, sweep_tx_id, parent_deposit_tx_id, amount_cents }
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

// ── Stripe Connect ────────────────────────────────────────────────────────────

export async function connectOnboard(key, returnUrl, refreshUrl) {
  const { body } = await request('/wallets/connect/onboard', {
    method: 'POST',
    key,
    body: { return_url: returnUrl || null, refresh_url: refreshUrl || null },
  })
  return body // { onboarding_url, account_id }
}

export async function getConnectStatus(key) {
  const { body, status } = await request('/wallets/connect/status', { key, throwOnError: false })
  if (status === 503) return { connected: false, charges_enabled: false, account_id: null, unavailable: true }
  return body // { connected, charges_enabled, account_id }
}

export async function withdrawFunds(key, amountCents) {
  const { body } = await request('/wallets/withdraw', {
    method: 'POST',
    key,
    body: { amount_cents: amountCents },
  })
  return body // { status, transfer_id, amount_cents }
}

export async function fetchPlatformStats() {
  const { body } = await request('/ops/platform-stats', {})
  return body
}

export async function fetchReconciliationRuns(key, limit = 5) {
  const { body } = await request(`/ops/payments/reconcile/runs?limit=${encodeURIComponent(String(limit))}`, { key })
  return body // { runs, count }
}
