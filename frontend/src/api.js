const BASE = '/api'

function headers(key) {
  return {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${key}`,
  }
}

export async function fetchHealth(key) {
  const r = await fetch(`${BASE}/health`, { headers: headers(key) })
  if (!r.ok) throw new Error(`${r.status}`)
  return r.json()
}

export async function fetchAgents(key) {
  const r = await fetch(`${BASE}/registry/agents`, { headers: headers(key) })
  if (!r.ok) throw new Error(`${r.status}`)
  return r.json()  // { agents: [...], count: N }
}

export async function fetchWalletMe(key) {
  const r = await fetch(`${BASE}/wallets/me`, { headers: headers(key) })
  if (!r.ok) throw new Error(`${r.status}`)
  return r.json()  // { wallet_id, owner_id, balance_cents, created_at, transactions: [...] }
}

export async function fetchWallet(key, walletId) {
  const r = await fetch(`${BASE}/wallets/${walletId}`, { headers: headers(key) })
  if (!r.ok) throw new Error(`${r.status}`)
  return r.json()
}

export async function depositToWallet(key, walletId, amountCents, memo = 'dashboard deposit') {
  const r = await fetch(`${BASE}/wallets/deposit`, {
    method: 'POST',
    headers: headers(key),
    body: JSON.stringify({ wallet_id: walletId, amount_cents: amountCents, memo }),
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || `${r.status}`)
  }
  return r.json()  // { tx_id, wallet_id, balance_cents }
}

export async function callAgent(key, agentId, payload) {
  const r = await fetch(`${BASE}/registry/agents/${agentId}/call`, {
    method: 'POST',
    headers: headers(key),
    body: JSON.stringify(payload),
  })
  const body = await r.json().catch(() => ({}))
  return { status: r.status, ok: r.ok, body }
}

export async function fetchRuns(key, limit = 50) {
  const r = await fetch(`${BASE}/runs?limit=${limit}`, { headers: headers(key) })
  if (!r.ok) return { runs: [] }
  return r.json()  // { runs: [...] }
}

export async function registerAgent(key, data) {
  const r = await fetch(`${BASE}/registry/register`, {
    method: 'POST',
    headers: headers(key),
    body: JSON.stringify(data),
  })
  if (!r.ok) {
    const body = await r.json().catch(() => ({}))
    throw new Error(body.detail || `${r.status}`)
  }
  return r.json()
}
