const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').trim()
const BASE = (RAW_BASE || '/api').replace(/\/+$/, '')
const VERSION = '1.0'
const CLIENT_ID = 'web-app'

let _onSessionExpired = null
export function setSessionExpiredHandler(fn) { _onSessionExpired = fn }

// Returns true only when the server explicitly identifies the key as invalid/expired.
// A plain 401 (e.g. transient server restart, brief config issue) is NOT included here
// so it goes through verifyAndExpire rather than immediately tearing down the session.
function isExplicitAuthFailure(status, parsedBody) {
  const code = typeof parsedBody?.error === 'string' ? parsedBody.error.trim().toUpperCase() : ''
  const message = typeof parsedBody?.message === 'string' ? parsedBody.message.trim().toLowerCase() : ''
  return (
    (status === 403 && code === 'INVALID_API_KEY')
    || message === 'api key is invalid or expired.'
  )
}

// Kept for makeApiError — marks the error object so callers (AuthContext) know
// it was a real auth failure, not a transient network problem.
function isInvalidApiKeyError(status, parsedBody) {
  return status === 401 || isExplicitAuthFailure(status, parsedBody)
}

// A 401 from a single data endpoint does NOT mean the session is dead — it
// could be a transient backend issue, a scope edge-case, or a race right after
// login. Before tearing down the session, verify with /auth/me. Only if that
// also 401s do we actually expire. Network errors during verification are
// ignored — keep the user signed in and let them retry.
let _verifyInFlight = null
async function verifyAndExpire(key) {
  if (!key || _verifyInFlight) return _verifyInFlight
  _verifyInFlight = (async () => {
    try {
      const probe = await fetch(`${BASE}/auth/me`, {
        headers: requestHeaders(key),
        signal: timeoutSignal(REQUEST_TIMEOUT_MS),
      })
      if (probe.status === 401 && _onSessionExpired) _onSessionExpired()
    } catch {
      // Network blip — do nothing. The session stays.
    } finally {
      _verifyInFlight = null
    }
  })()
  return _verifyInFlight
}

function makeRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID()
  return `req_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 12)}`
}

function requestHeaders(key, { idempotencyKey, requestId } = {}) {
  const out = {
    'Content-Type': 'application/json',
    'X-Aztea-Version': VERSION,
    'X-Aztea-Client': CLIENT_ID,
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
    // Prefer human-readable fields the server sets on structured errors
    // (e.g. dispute 500 returns {error, phase, exception_type, message}).
    if (typeof detail.message === 'string' && detail.message.trim()) {
      return detail.message
    }
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
// Exported so `frontend/src/utils/errorCopy.js` can reuse the same code → copy
// lookup without re-declaring it; api.js stays the single source of truth.
export const API_ERROR_MESSAGE_BY_CODE = {
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
  'job.create_failed': 'Job could not be created. Your charge was refunded. Retry shortly.',
  'dispute.filing_failed': 'Dispute could not be filed. Your evidence is still here — retry, or check the dispute window.',
  'registry.manifest_unreachable': 'We couldn’t reach that manifest URL. Confirm it’s publicly fetchable and retry.',
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
  err.authInvalid = isInvalidApiKeyError(response.status, parsedBody)
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
    if (_onSessionExpired && !path.startsWith('/auth/') && key) {
      if (isExplicitAuthFailure(response.status, parsedBody)) {
        // Server explicitly rejected the key — fail closed immediately.
        _onSessionExpired()
      } else if (response.status === 401) {
        // Ambiguous 401 (transient server issue, restart, etc.) — verify with
        // /auth/me before tearing down the session. See verifyAndExpire above.
        verifyAndExpire(key)
      }
    }
    throw makeApiError(response, parsedBody)
  }
  return {
    ok: response.ok,
    status: response.status,
    body: parsedBody,
    headers: response.headers,
  }
}

// ── Web playground (public, anonymous; /scrape + /web/verify) ───────────────────

// POST /scrape — anonymous. Returns 200 with {success, data} | {success:false,error};
// 503 when the web API is disabled. throwOnError:false so the page renders inline.
export async function scrapeWeb(url, formats = ['markdown']) {
  const { ok, status, body } = await request('/scrape', {
    method: 'POST', body: { url, formats }, throwOnError: false,
  })
  return { ok, status, body }
}

// POST /web/verify — verify a signed observation receipt without re-crawling.
export async function verifyWebReceipt(receipt) {
  const { body } = await request('/web/verify', {
    method: 'POST', body: { receipt }, throwOnError: true,
  })
  return body // {valid, checks?, claim, note?, error?}
}

// ── Auth ──────────────────────────────────────────────────────────────────────

export async function authRegister(username, email, password, role = 'both') {
  const { body } = await request('/auth/register', {
    method: 'POST',
    body: { username, email, password, role },
  })
  return body
}

export async function authSignupStart(username, email, password, role = 'both') {
  const { body } = await request('/auth/signup/start', {
    method: 'POST',
    body: { username, email, password, role },
  })
  return body
}

export async function authSignupVerify(email, otp) {
  const { body } = await request('/auth/signup/verify', {
    method: 'POST',
    body: { email, otp },
  })
  return body
}

export async function authSignupResend(email) {
  const { body } = await request('/auth/signup/resend', {
    method: 'POST',
    body: { email },
  })
  return body
}

export async function authUpdateRole(key, role) {
  const { body } = await request('/auth/role', {
    method: 'PATCH',
    key,
    body: { role },
  })
  return body
}

export async function authUpdateProfile(key, fields) {
  const { body } = await request('/auth/me', {
    method: 'PATCH',
    key,
    body: fields,
  })
  return body
}

export async function authChangePassword(key, current_password, new_password) {
  const { body } = await request('/auth/change-password', {
    method: 'POST',
    key,
    body: { current_password, new_password },
  })
  return body
}

export async function listBillingTopups(key, limit = 25) {
  const { body } = await request(`/billing/topups?limit=${encodeURIComponent(limit)}`, { key })
  return body
}

export async function createBillingSetupSession(key) {
  const { body } = await request('/billing/setup-session', {
    method: 'POST',
    key,
  })
  return body
}

export async function listBillingPaymentMethods(key) {
  const { body } = await request('/billing/payment-methods', { key })
  return body
}

export async function deleteBillingPaymentMethod(key, paymentMethodId) {
  const { body } = await request(`/billing/payment-methods/${encodeURIComponent(paymentMethodId)}`, {
    method: 'DELETE',
    key,
  })
  return body
}

export async function authLogin(email, password) {
  const { body } = await request('/auth/login', {
    method: 'POST',
    // rotate=true asks the server to mint and return a fresh session key
    // without revoking the user's other live sessions (see login_user in
    // core/auth/users.py). The web app needs a real raw_api_key on sign-in
    // because a fresh browser has nothing cached locally.
    body: { email, password, rotate: true },
  })
  return body
}

export async function authGoogle(idToken) {
  const { body } = await request('/auth/google', {
    method: 'POST',
    body: { id_token: idToken },
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

export async function createAuthKey(key, name, scopes = ['caller', 'worker'], options = {}) {
  const payload = { name, scopes }
  if (options.per_job_cap_cents != null) payload.per_job_cap_cents = options.per_job_cap_cents
  if (options.max_spend_cents != null) payload.max_spend_cents = options.max_spend_cents
  const { body } = await request('/auth/keys', {
    method: 'POST',
    key,
    body: payload,
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

export async function askPublicDocs(question, docSlug = null) {
  const { body } = await request('/public/docs/ask', {
    method: 'POST',
    body: { question: String(question ?? ''), doc_slug: docSlug || null },
  })
  return body
}

// ── Hosted skills ─────────────────────────────────────────────────────────────

export async function validateSkillMd(key, skillMd) {
  const { body } = await request('/skills/validate', {
    method: 'POST',
    key,
    body: { skill_md: skillMd },
  })
  return body
}

export async function createSkill(key, skillMd, pricePerCallUsd) {
  const { body } = await request('/skills', {
    method: 'POST',
    key,
    body: { skill_md: skillMd, price_per_call_usd: pricePerCallUsd },
  })
  return body
}

export async function fetchMySkills(key) {
  const { body } = await request('/skills', { key })
  return body
}

export async function deleteSkill(key, skillId) {
  const { body } = await request(`/skills/${skillId}`, { method: 'DELETE', key })
  return body
}

// ── Registry ──────────────────────────────────────────────────────────────────

export async function fetchAgents(key, tag, { rankBy = 'trust', ownerId } = {}) {
  const params = new URLSearchParams()
  if (tag) params.set('tag', tag)
  if (rankBy) params.set('rank_by', rankBy)
  // Wave 2 (2026-05-26): owner_id filter powers the builder profile page.
  // Empty string is treated as "no filter" — same convention as tag/rankBy.
  if (ownerId) params.set('owner_id', ownerId)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const { body } = await request(`/registry/agents${suffix}`, { key })
  return body
}

// Wave 2 (2026-05-26): public builder profile lookup. PUBLIC — no API
// key required. Backed by GET /registry/builders/{username}; the route
// aggregates agent count, total calls, average rating, trust score, and
// (only when the builder opted in) total earnings.
export async function fetchBuilder(username) {
  if (!username) throw new Error('username is required')
  const { body } = await request(`/registry/builders/${encodeURIComponent(username)}`)
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

// Plan B Phase 1 (2026-05-27): rotate the per-agent HMAC signing secret.
// The new value is returned ONCE in the response body. Caller must surface
// it to the owner immediately — the secret cannot be re-displayed.
export async function rotateAgentEndpointSecret(key, agentId) {
  const { body } = await request(`/registry/agents/${agentId}/rotate-secret`, {
    method: 'POST',
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
  // Returns the dispute for this job, or null if none exists / any error
  const { body, status } = await request(`/jobs/${jobId}/dispute`, { key, throwOnError: false })
  if (status === 200) return body
  return null
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

// Mints a short-lived HMAC token for the Elixir realtime WebSocket. Returns
// { token, expires_at }, or null if the deployment hasn't been configured
// (503 from the server). Callers must treat 503 as a benign signal — the
// existing SSE + polling fallbacks continue to work.
export async function fetchSocketToken(key) {
  try {
    const { body } = await request('/auth/socket-token', { method: 'POST', key })
    if (body && typeof body.token === 'string') return body
    return null
  } catch (err) {
    if (err?.status === 503) return null
    throw err
  }
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

// ── Cryptographic identity (no auth) ─────────────────────────────────────────

export async function fetchAgentDidDocument(agentId) {
  // The DID document is W3C-spec, served at the URL the DID itself resolves to.
  // No auth required — the public key is by design publicly discoverable.
  const { body, status } = await request(
    `/agents/${encodeURIComponent(agentId)}/did.json`,
    { throwOnError: false },
  )
  if (status === 404) return null
  return body
}

export async function fetchJobSignature(jobId) {
  // Signature endpoint is unauthenticated — anyone with the job id should be
  // able to verify the output. Returns null if the job has no signature yet.
  const { body, status } = await request(
    `/jobs/${encodeURIComponent(jobId)}/signature`,
    { throwOnError: false },
  )
  if (status === 404) return null
  return body
}

export async function createAgentCallerKey(key, agentId, name = 'Caller key') {
  const { body } = await request(
    `/registry/agents/${encodeURIComponent(agentId)}/caller-keys`,
    { method: 'POST', key, body: { name } },
  )
  return body // { key_id, agent_id, raw_key, key_prefix, key_type, created_at }
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

// ── Pipelines / recipes ───────────────────────────────────────────────────

export async function runPipeline(key, pipelineId, inputPayload) {
  const { body } = await request(`/pipelines/${encodeURIComponent(pipelineId)}/run`, {
    key,
    method: 'POST',
    body: { input_payload: inputPayload },
  })
  return body // { run_id, pipeline_id, status }
}

export async function fetchPipelineRun(key, pipelineId, runId) {
  const { body } = await request(
    `/pipelines/${encodeURIComponent(pipelineId)}/runs/${encodeURIComponent(runId)}`,
    { key }
  )
  return body
}

// Helper: poll until terminal. Returns the final run record.
export async function awaitPipelineRun(key, pipelineId, runId, { intervalMs = 1500, timeoutMs = 120000 } = {}) {
  const terminal = new Set(['complete', 'completed', 'failed', 'cancelled', 'error'])
  const started = Date.now()
  while (true) {
    const run = await fetchPipelineRun(key, pipelineId, runId)
    if (terminal.has(String(run?.status || '').toLowerCase())) return run
    if (Date.now() - started > timeoutMs) {
      const err = new Error('Pipeline run timed out.')
      err.status = 504
      throw err
    }
    await new Promise(resolve => setTimeout(resolve, intervalMs))
  }
}

// List built-in + caller-owned recipes for the Workflows discovery page.
// Each entry carries slug + steps + estimated_total_cost_usd so the UI can
// render the catalog without N+1 round-trips for per-step agent prices.
export async function fetchRecipes(key) {
  const { body } = await request('/recipes', { key })
  return body
}

// Run a built-in or user-owned recipe. The server resolves the recipe_id to
// a pipeline and returns { run_id, pipeline_id, recipe_id, status }.
export async function runRecipe(key, recipeId, inputPayload) {
  const { body } = await request(`/recipes/${encodeURIComponent(recipeId)}/run`, {
    key,
    method: 'POST',
    body: { input_payload: inputPayload },
  })
  return body
}

// Workspaces (v0/v0.1). The server's GET /workspaces returns the caller's
// own workspaces ordered newest-first. The detail view fetches a single
// workspace + its artifacts + (when sealed) its public manifest.
export async function fetchWorkspaceList(key, limit = 100) {
  const { body } = await request(`/workspaces?limit=${limit}`, { key })
  return body
}

export async function fetchWorkspace(key, workspaceId) {
  const { body } = await request(`/workspaces/${encodeURIComponent(workspaceId)}`, { key })
  return body
}

export async function fetchWorkspaceArtifacts(key, workspaceId) {
  const { body } = await request(`/workspaces/${encodeURIComponent(workspaceId)}/artifacts`, { key })
  return body
}

// Public — no key needed. Returns { manifest, signature, public_key_did }.
export async function fetchWorkspaceManifest(workspaceId) {
  const { body } = await request(`/workspaces/${encodeURIComponent(workspaceId)}/manifest`, {})
  return body
}

// Public — no key needed. Returns { valid, signer_did, sealed_at }.
export async function verifyWorkspaceSeal(workspaceId) {
  const { body } = await request(`/workspaces/${encodeURIComponent(workspaceId)}/verify`, {
    method: 'POST',
  })
  return body
}

export async function deleteWorkspace(key, workspaceId) {
  await request(`/workspaces/${encodeURIComponent(workspaceId)}`, {
    key,
    method: 'DELETE',
  })
}


// ── Browser playground (Wave 3) ───────────────────────────────────────────────

// Run a buyer-supplied handler in the sandbox. Anonymous-callable —
// `key` is optional. Returns { execution_id, exit_code, timed_out,
// stdout, stderr, execution_time_ms, error }. The server enforces a
// 5/minute IP-rate-limit + listing-safety scan before the sandbox spawns.
export async function playgroundTest({ key, source, inputPayload, timeoutS = 5 }) {
  const { body } = await request('/api/playground/test', {
    method: 'POST',
    key,
    body: {
      source,
      input_payload: inputPayload ?? {},
      timeout_s: timeoutS,
    },
    throwOnError: false,
  })
  return body
}

// Publish a SKILL.md as a hosted agent. Requires worker scope. The
// playground endpoint 308-redirects to /skills which carries the full
// publish pipeline (listing-safety + LLM judge + agent registration).
export async function playgroundPublish({ key, skillMd, pricePerCallUsd, extra = {} }) {
  // The 308 redirect from /api/playground/publish → /skills is honored
  // by fetch() automatically. The browser preserves the method + body
  // per RFC 7538.
  const { body } = await request('/skills', {
    method: 'POST',
    key,
    body: {
      skill_md: skillMd,
      price_per_call_usd: pricePerCallUsd,
      ...extra,
    },
    throwOnError: false,
  })
  return body
}
