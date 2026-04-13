import { useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMarket } from '../context/MarketContext'
import { createAuthKey, deleteAuthKey, depositToWallet, fetchAuthKeys, rotateAuthKey } from '../api'
import CallWorkspace from './CallWorkspace'
import ActivityPanel from './ActivityPanel'
import RegisterAgentModal from './RegisterAgentModal'

// ── Helpers ────────────────────────────────────────────────────────────────────

function usdFormat(cents) {
  return `$${(cents / 100).toFixed(2)}`
}

function StatusDot({ color = 'var(--positive)', pulse = false }) {
  return (
    <span style={{
      position: 'relative', display: 'inline-flex',
      width: 7, height: 7, flexShrink: 0,
    }}>
      <span style={{
        position: 'absolute', inset: 0,
        borderRadius: '50%', background: color,
        ...(pulse ? { animation: 'ping 1.5s cubic-bezier(0,0,0.2,1) infinite' } : {}),
        opacity: pulse ? 0.75 : 1,
      }} />
      <span style={{ borderRadius: '50%', background: color, width: '100%', height: '100%', position: 'relative' }} />
    </span>
  )
}

// ── Wallet widget ──────────────────────────────────────────────────────────────
function WalletWidget() {
  const { apiKey, wallet, refreshWallet, showToast } = useMarket()
  const [amountDollars, setAmountDollars] = useState('')
  const [open, setOpen] = useState(false)
  const [working, setWorking] = useState(false)

  const deposit = async e => {
    e?.preventDefault()
    const dollars = parseFloat(amountDollars)
    if (!dollars || dollars <= 0) return
    const cents = Math.round(dollars * 100)
    setWorking(true)
    try {
      await depositToWallet(apiKey, wallet.wallet_id, cents)
      await refreshWallet()
      showToast(`Added ${usdFormat(cents)} to your wallet`, 'success')
      setAmountDollars(''); setOpen(false)
    } catch (err) {
      showToast(err.message, 'error')
    } finally { setWorking(false) }
  }

  if (!wallet) return null
  const isLow = wallet.balance_cents < 50  // < $0.50

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 12px', borderRadius: 'var(--radius-md)',
          background: 'var(--surface-2)',
          border: `1px solid ${isLow ? 'var(--neutral-border)' : 'var(--border)'}`,
          cursor: 'pointer', transition: 'border-color 0.15s',
        }}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none"
          stroke={isLow ? 'var(--neutral-color)' : 'var(--text-muted)'} strokeWidth="2">
          <rect x="2" y="5" width="20" height="14" rx="2"/>
          <path d="M16 12h.01"/>
        </svg>
        <span style={{
          fontFamily: 'var(--font-mono)', fontWeight: 700,
          color: isLow ? 'var(--neutral-color)' : 'var(--text-primary)', fontSize: 13,
        }}>
          {usdFormat(wallet.balance_cents)}
        </span>
        <span style={{ color: 'var(--brand)', fontSize: 11, fontWeight: 600 }}>+ Add</span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.97 }}
            transition={{ duration: 0.15 }}
            style={{
              position: 'absolute', top: 'calc(100% + 8px)', right: 0,
              background: 'var(--surface)',
              border: '1px solid var(--border-bright)',
              borderRadius: 'var(--radius-lg)',
              boxShadow: 'var(--shadow-lg)',
              padding: 18, minWidth: 260, zIndex: 60,
            }}
          >
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.05em', textTransform: 'uppercase', marginBottom: 4 }}>
                Wallet balance
              </div>
              <div style={{ fontSize: 28, fontFamily: 'var(--font-mono)', fontWeight: 700, color: 'var(--text-primary)', letterSpacing: '-0.02em' }}>
                {usdFormat(wallet.balance_cents)}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                {wallet.balance_cents}¢ available
              </div>
              {isLow && (
                <div style={{ fontSize: 11, color: 'var(--neutral-color)', marginTop: 6, display: 'flex', gap: 5, alignItems: 'center' }}>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                    <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
                    <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                  </svg>
                  Low balance — add funds
                </div>
              )}
            </div>
            <form onSubmit={deposit} style={{ display: 'flex', gap: 8 }}>
              <div style={{ flex: 1, position: 'relative' }}>
                <span style={{
                  position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)',
                  color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 13,
                }}>$</span>
                <input
                  autoFocus
                  type="number" min="0.01" step="0.01"
                  placeholder="5.00"
                  value={amountDollars}
                  onChange={e => setAmountDollars(e.target.value)}
                  className="input-base"
                  style={{ paddingLeft: 24, fontFamily: 'var(--font-mono)' }}
                />
              </div>
              <button type="submit" disabled={working || !amountDollars} className="btn-brand"
                style={{ padding: '8px 14px', fontSize: 12, whiteSpace: 'nowrap' }}>
                {working ? '…' : 'Add'}
              </button>
            </form>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Agent card ─────────────────────────────────────────────────────────────────
function AgentCard({ agent, selected, onClick }) {
  const successRate = agent.total_calls > 0 ? Math.round(agent.success_rate * 100) : null
  const statusColor = successRate === null ? 'var(--text-muted)' :
    successRate >= 90 ? 'var(--positive)' :
    successRate >= 60 ? 'var(--neutral-color)' : 'var(--negative)'

  return (
    <button
      onClick={() => onClick(agent)}
      style={{
        width: '100%', textAlign: 'left',
        padding: '13px 14px',
        borderRadius: 'var(--radius-md)',
        border: `1px solid ${selected ? 'var(--brand-border)' : 'var(--border)'}`,
        background: selected ? 'var(--brand-light)' : 'var(--surface-2)',
        cursor: 'pointer',
        transition: 'border-color 0.12s, background 0.12s, box-shadow 0.12s',
        boxShadow: selected ? 'var(--shadow-brand)' : 'none',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
        <span style={{
          fontWeight: 600, fontSize: 12.5, lineHeight: 1.35,
          color: selected ? 'var(--brand)' : 'var(--text-primary)',
          fontFamily: 'var(--font-display)',
          flex: 1, minWidth: 0, paddingRight: 8,
        }}>
          {agent.name}
        </span>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 700,
          color: selected ? 'var(--brand)' : 'var(--text-secondary)',
          flexShrink: 0,
          background: selected ? 'rgba(0,212,168,0.12)' : 'var(--bg)',
          padding: '2px 7px', borderRadius: 4,
          border: '1px solid var(--border)',
        }}>
          ${agent.price_per_call_usd.toFixed(3)}
        </span>
      </div>
      <p style={{
        fontSize: 11, color: 'var(--text-muted)', lineHeight: 1.5, marginBottom: 9,
        display: '-webkit-box', WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical', overflow: 'hidden',
      }}>
        {agent.description}
      </p>
      <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <StatusDot color={statusColor} />
          <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>
            {successRate !== null ? `${successRate}% success` : 'New'}
          </span>
        </div>
        {agent.avg_latency_ms > 0 && (
          <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>
            {(agent.avg_latency_ms / 1000).toFixed(1)}s avg
          </span>
        )}
        <span style={{ fontSize: 10.5, color: 'var(--text-muted)', marginLeft: 'auto' }}>
          {agent.total_calls} call{agent.total_calls !== 1 ? 's' : ''}
        </span>
      </div>
    </button>
  )
}

// ── Toast ──────────────────────────────────────────────────────────────────────
function Toast() {
  const { toast } = useMarket()
  const styles = {
    success: { bg: 'var(--positive-bg)', border: 'var(--positive-border)', text: 'var(--positive)' },
    error:   { bg: 'var(--negative-bg)', border: 'var(--negative-border)', text: 'var(--negative)' },
    info:    { bg: 'var(--brand-light)',  border: 'var(--brand-border)',    text: 'var(--brand)' },
  }
  const s = styles[toast?.type] ?? styles.info
  return (
    <AnimatePresence>
      {toast && (
        <motion.div
          key={toast.id}
          initial={{ opacity: 0, y: -12, x: '-50%' }}
          animate={{ opacity: 1, y: 0, x: '-50%' }}
          exit={{ opacity: 0, y: -8, x: '-50%' }}
          transition={{ duration: 0.2 }}
          style={{
            position: 'fixed', top: 64, left: '50%',
            background: s.bg, border: `1px solid ${s.border}`,
            borderRadius: 'var(--radius-md)', color: s.text,
            fontSize: 13, fontWeight: 600,
            padding: '9px 16px',
            boxShadow: 'var(--shadow-md)',
            zIndex: 300, pointerEvents: 'none',
            maxWidth: 380, textAlign: 'center',
          }}
        >
          {toast.msg}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── User badge ─────────────────────────────────────────────────────────────────
function UserBadge({ user, onSignOut }) {
  const { apiKey, showToast } = useMarket()
  const [open, setOpen] = useState(false)
  const [keys, setKeys] = useState(null)
  const [copied, setCopied] = useState(false)
  const [working, setWorking] = useState(false)
  const [newKeyName, setNewKeyName] = useState('')
  const [newKeyScopes, setNewKeyScopes] = useState({ caller: true, worker: true, admin: false })
  const [revealedKey, setRevealedKey] = useState('')
  const [error, setError] = useState('')
  const initial = (user?.username ?? 'A')[0].toUpperCase()
  const currentPrefix = (apiKey ?? '').slice(0, 12)

  const selectedScopes = ['caller', 'worker', 'admin'].filter(scope => newKeyScopes[scope])

  const loadKeys = useCallback(async (force = false) => {
    if (keys && !force) return
    try {
      setError('')
      const result = await fetchAuthKeys(apiKey)
      setKeys(result.keys ?? [])
    } catch (err) {
      setError(err.message)
      setKeys([])
    }
  }, [apiKey, keys])

  const handleOpen = () => {
    setOpen(o => {
      if (!o) loadKeys()
      return !o
    })
  }

  const copyKey = async rawKey => {
    await navigator.clipboard.writeText(rawKey)
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  const createKey = async () => {
    if (working) return
    if (!selectedScopes.length) {
      setError('Select at least one scope.')
      return
    }
    setWorking(true)
    setError('')
    try {
      const created = await createAuthKey(
        apiKey,
        newKeyName.trim() || 'New key',
        selectedScopes,
      )
      setRevealedKey(created.raw_key || '')
      setNewKeyName('')
      await loadKeys(true)
      showToast('API key created', 'success')
    } catch (err) {
      setError(err.message)
    } finally {
      setWorking(false)
    }
  }

  const rotateKey = async keyId => {
    if (working) return
    setWorking(true)
    setError('')
    try {
      const rotated = await rotateAuthKey(apiKey, keyId)
      setRevealedKey(rotated.raw_key || '')
      await loadKeys(true)
      showToast('API key rotated', 'success')
    } catch (err) {
      setError(err.message)
    } finally {
      setWorking(false)
    }
  }

  const revokeKey = async keyId => {
    if (working) return
    setWorking(true)
    setError('')
    try {
      await deleteAuthKey(apiKey, keyId)
      await loadKeys(true)
      showToast('API key revoked', 'success')
    } catch (err) {
      setError(err.message)
    } finally {
      setWorking(false)
    }
  }

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={handleOpen}
        style={{
          width: 32, height: 32, borderRadius: '50%',
          background: 'var(--brand)', color: 'var(--text-inverse)',
          fontWeight: 800, fontSize: 13, fontFamily: 'var(--font-display)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', border: '2px solid rgba(0,212,168,0.3)',
          boxShadow: 'var(--shadow-brand)', flexShrink: 0,
        }}
      >
        {initial}
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.13 }}
            style={{
              position: 'absolute', top: 'calc(100% + 8px)', right: 0,
              background: 'var(--surface)',
              border: '1px solid var(--border-bright)',
              borderRadius: 'var(--radius-lg)',
              boxShadow: 'var(--shadow-lg)',
              minWidth: 360, zIndex: 60, overflow: 'hidden',
            }}
          >
            {/* User info */}
            <div style={{ padding: '12px 14px', borderBottom: '1px solid var(--border)' }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)', fontFamily: 'var(--font-display)' }}>
                {user?.username ?? 'Agent'}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 1 }}>
                {user?.email ?? ''}
              </div>
            </div>

            {/* API key display */}
            <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.05em', textTransform: 'uppercase', marginBottom: 6 }}>
                Current session key
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <code style={{
                  fontFamily: 'var(--font-mono)', fontSize: 11,
                  color: 'var(--text-secondary)',
                  background: 'var(--bg)', padding: '3px 7px',
                  borderRadius: 4, border: '1px solid var(--border)',
                  flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {currentPrefix}••••••••
                </code>
                <button
                  onClick={() => copyKey(apiKey)}
                  style={{
                    flexShrink: 0, padding: '3px 8px',
                    background: 'var(--surface-2)',
                    border: '1px solid var(--border)',
                    borderRadius: 4, cursor: 'pointer',
                    fontSize: 10, color: copied ? 'var(--positive)' : 'var(--text-muted)',
                    fontFamily: 'var(--font-sans)', fontWeight: 600,
                    transition: 'color 0.15s',
                  }}
                >
                  {copied ? 'Copied!' : 'Copy'}
                </button>
              </div>
            </div>

            {/* Key management */}
            <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
              <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', letterSpacing: '0.05em', textTransform: 'uppercase', marginBottom: 8 }}>
                API keys
              </div>

              {keys === null ? (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>Loading…</div>
              ) : keys.length === 0 ? (
                <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>No active keys</div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 7, marginBottom: 10, maxHeight: 150, overflowY: 'auto', paddingRight: 2 }}>
                  {keys.map(k => {
                    const isCurrentSessionKey = k.key_prefix === currentPrefix
                    return (
                      <div key={k.key_id} style={{
                        border: '1px solid var(--border)',
                        background: 'var(--surface-2)',
                        borderRadius: 6,
                        padding: '7px 8px',
                      }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 8 }}>
                          <div style={{ minWidth: 0 }}>
                            <div style={{ fontSize: 11.5, fontWeight: 700, color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                              {k.name}
                            </div>
                            <code style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                              {k.key_prefix}••••••••
                            </code>
                          </div>
                          <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                            <button
                              onClick={() => rotateKey(k.key_id)}
                              disabled={working || isCurrentSessionKey}
                              title={isCurrentSessionKey ? 'Rotate a non-session key to avoid immediate logout.' : 'Rotate key'}
                              style={{
                                padding: '2px 6px',
                                borderRadius: 4,
                                border: '1px solid var(--border)',
                                background: 'var(--bg)',
                                color: 'var(--text-secondary)',
                                fontSize: 10,
                                cursor: working || isCurrentSessionKey ? 'not-allowed' : 'pointer',
                                opacity: working || isCurrentSessionKey ? 0.5 : 1,
                              }}
                            >
                              Rotate
                            </button>
                            <button
                              onClick={() => revokeKey(k.key_id)}
                              disabled={working || isCurrentSessionKey}
                              title={isCurrentSessionKey ? 'Cannot revoke current session key from this session.' : 'Revoke key'}
                              style={{
                                padding: '2px 6px',
                                borderRadius: 4,
                                border: '1px solid var(--negative-border)',
                                background: 'var(--negative-bg)',
                                color: 'var(--negative)',
                                fontSize: 10,
                                cursor: working || isCurrentSessionKey ? 'not-allowed' : 'pointer',
                                opacity: working || isCurrentSessionKey ? 0.5 : 1,
                              }}
                            >
                              Revoke
                            </button>
                          </div>
                        </div>
                        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 5 }}>
                          {(k.scopes ?? []).map(scope => (
                            <span key={`${k.key_id}-${scope}`} style={{
                              fontSize: 9.5,
                              padding: '1px 6px',
                              borderRadius: 10,
                              border: '1px solid var(--brand-border)',
                              background: 'var(--brand-light)',
                              color: 'var(--brand)',
                              textTransform: 'uppercase',
                              fontWeight: 700,
                              letterSpacing: '0.04em',
                            }}>
                              {scope}
                            </span>
                          ))}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}

              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <input
                  value={newKeyName}
                  onChange={e => setNewKeyName(e.target.value)}
                  placeholder="New key name"
                  className="input-base"
                  style={{ fontSize: 11.5, padding: '7px 9px' }}
                />
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {['caller', 'worker', 'admin'].map(scope => (
                    <label key={scope} style={{
                      display: 'inline-flex',
                      gap: 4,
                      alignItems: 'center',
                      fontSize: 10.5,
                      color: 'var(--text-secondary)',
                      border: '1px solid var(--border)',
                      borderRadius: 20,
                      padding: '2px 7px',
                      background: 'var(--bg)',
                      textTransform: 'uppercase',
                    }}>
                      <input
                        type="checkbox"
                        checked={newKeyScopes[scope]}
                        onChange={e => setNewKeyScopes(prev => ({ ...prev, [scope]: e.target.checked }))}
                      />
                      {scope}
                    </label>
                  ))}
                </div>
                <button
                  onClick={createKey}
                  disabled={working}
                  className="btn-brand"
                  style={{ fontSize: 11.5, padding: '7px 0' }}
                >
                  {working ? 'Working…' : 'Create key'}
                </button>
              </div>

              {!!revealedKey && (
                <div style={{
                  marginTop: 8,
                  padding: '8px 9px',
                  background: 'var(--neutral-bg)',
                  border: '1px solid var(--neutral-border)',
                  borderRadius: 6,
                  fontSize: 10.5,
                  color: 'var(--neutral-color)',
                }}>
                  New key (shown once):
                  <code style={{
                    display: 'block',
                    marginTop: 4,
                    fontFamily: 'var(--font-mono)',
                    fontSize: 10,
                    color: 'var(--text-secondary)',
                    overflowX: 'auto',
                    whiteSpace: 'nowrap',
                  }}>
                    {revealedKey}
                  </code>
                </div>
              )}
              {!!error && (
                <div style={{ marginTop: 8, fontSize: 10.5, color: 'var(--negative)' }}>
                  {error}
                </div>
              )}
            </div>

            <button
              onClick={() => { setOpen(false); onSignOut() }}
              style={{
                width: '100%', padding: '10px 14px', textAlign: 'left',
                fontSize: 13, color: 'var(--negative)', cursor: 'pointer',
                borderRadius: 0, fontFamily: 'var(--font-sans)',
                transition: 'background 0.1s',
              }}
            >
              Sign out
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Sidebar search ─────────────────────────────────────────────────────────────
function AgentSearch({ value, onChange }) {
  return (
    <div style={{ position: 'relative' }}>
      <svg style={{ position: 'absolute', left: 10, top: '50%', transform: 'translateY(-50%)', pointerEvents: 'none' }}
        width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input
        type="text"
        placeholder="Search agents…"
        value={value}
        onChange={e => onChange(e.target.value)}
        className="input-base"
        style={{ paddingLeft: 30, fontSize: 12.5 }}
      />
    </div>
  )
}

// ── Main dashboard ─────────────────────────────────────────────────────────────
export default function Dashboard({ onSignOut, user }) {
  const { agents, loading } = useMarket()
  const [selectedAgent, setSelectedAgent] = useState(null)
  const [activeTab, setActiveTab]         = useState('activity')
  const [search, setSearch]               = useState('')
  const [showRegister, setShowRegister]   = useState(false)

  const agent = selectedAgent ?? agents[0] ?? null

  const filteredAgents = agents.filter(a =>
    !search || a.name.toLowerCase().includes(search.toLowerCase()) ||
    (a.tags ?? []).some(t => t.includes(search.toLowerCase()))
  )

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--bg)' }}>

      {/* ── Header ── */}
      <header style={{
        height: 'var(--header-h)', flexShrink: 0,
        background: 'var(--surface)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 18px', position: 'sticky', top: 0, zIndex: 20,
        boxShadow: '0 1px 0 var(--border)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              width: 24, height: 24, borderRadius: 6,
              background: 'var(--brand)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: 'var(--shadow-brand)',
            }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--text-inverse)" strokeWidth="2.5">
                <circle cx="12" cy="12" r="3"/>
                <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
              </svg>
            </div>
            <span style={{ fontWeight: 800, fontSize: 14, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' }}>
              agentmarket
            </span>
          </div>

          <div style={{
            display: 'flex', alignItems: 'center', gap: 5,
            padding: '3px 10px', borderRadius: 20,
            background: 'var(--positive-bg)', border: '1px solid var(--positive-border)',
          }}>
            <StatusDot pulse />
            <span style={{ fontSize: 10.5, color: 'var(--positive)', fontWeight: 700 }}>
              {agents.length} agent{agents.length !== 1 ? 's' : ''} online
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <WalletWidget />
          <UserBadge user={user} onSignOut={onSignOut} />
        </div>
      </header>

      {/* ── Body ── */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0 }}>

        {/* ── Sidebar ── */}
        <aside style={{
          width: 'var(--sidebar-w)', flexShrink: 0,
          borderRight: '1px solid var(--border)',
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
          background: 'var(--surface)',
        }}>
          <div style={{ padding: '12px 12px 8px', borderBottom: '1px solid var(--border)' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
              <h2 style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                Agent Registry
              </h2>
              <span style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>
                {agents.length} listed
              </span>
            </div>
            <AgentSearch value={search} onChange={setSearch} />
          </div>

          <div style={{ flex: 1, overflowY: 'auto', padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 5 }}>
            {loading ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '6px 0' }}>
                {[1, 2, 3].map(i => (
                  <div key={i} style={{
                    height: 88, borderRadius: 'var(--radius-md)',
                    background: 'var(--surface-2)', animation: 'pulse 1.5s ease-in-out infinite',
                    animationDelay: `${i * 0.1}s`,
                  }} />
                ))}
              </div>
            ) : filteredAgents.length === 0 ? (
              <div style={{ padding: '24px 8px', textAlign: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
                {search ? `No agents matching "${search}"` : 'No agents registered yet.'}
              </div>
            ) : filteredAgents.map((a, idx) => (
              <motion.div
                key={a.agent_id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.2, delay: idx * 0.04 }}
              >
                <AgentCard
                  agent={a}
                  selected={agent?.agent_id === a.agent_id}
                  onClick={setSelectedAgent}
                />
              </motion.div>
            ))}
          </div>

          {/* Register CTA */}
          <div style={{ padding: '10px 12px', borderTop: '1px solid var(--border)', background: 'var(--bg)' }}>
            <p style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.5 }}>
              List your agent — earn <span style={{ color: 'var(--brand)', fontWeight: 600 }}>90%</span> per call
            </p>
            <button
              onClick={() => setShowRegister(true)}
              style={{
                display: 'block', width: '100%', textAlign: 'center',
                padding: '7px 0', borderRadius: 'var(--radius-md)',
                border: '1px solid var(--brand-border)',
                background: 'var(--brand-light)',
                color: 'var(--brand)', fontSize: 12, fontWeight: 700,
                cursor: 'pointer', fontFamily: 'var(--font-sans)',
                transition: 'background 0.15s',
              }}
            >
              + Register an agent
            </button>
          </div>
        </aside>

        {/* ── Main content ── */}
        <main style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>

          {/* Call workspace */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '22px 26px 0' }}>
            {agent ? (
              <CallWorkspace agent={agent} />
            ) : (
              <div style={{
                display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center',
                height: '100%', gap: 12, textAlign: 'center',
              }}>
                <div style={{
                  width: 48, height: 48, borderRadius: 14,
                  background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                }}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" strokeWidth="1.8">
                    <circle cx="12" cy="12" r="3"/>
                    <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
                  </svg>
                </div>
                <div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: 'var(--text-primary)', fontFamily: 'var(--font-display)', marginBottom: 4 }}>
                    Select an agent
                  </div>
                  <div style={{ fontSize: 12, color: 'var(--text-muted)', maxWidth: 260, lineHeight: 1.6 }}>
                    Browse the registry on the left and click an agent to call it.
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Bottom panel */}
          <div style={{ flexShrink: 0, borderTop: '1px solid var(--border)', background: 'var(--surface)' }}>
            <div style={{ display: 'flex', borderBottom: '1px solid var(--border)', padding: '0 18px' }}>
              {[
                { id: 'activity',  label: 'Transactions' },
                { id: 'jobs',      label: 'Jobs' },
                { id: 'analytics', label: 'Analytics' },
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  style={{
                    padding: '9px 14px', fontSize: 12, fontWeight: 600,
                    color: activeTab === tab.id ? 'var(--brand)' : 'var(--text-muted)',
                    borderBottom: `2px solid ${activeTab === tab.id ? 'var(--brand)' : 'transparent'}`,
                    cursor: 'pointer', transition: 'color 0.15s', marginBottom: -1,
                    fontFamily: 'var(--font-sans)',
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <div style={{ height: 230, overflowY: 'auto' }}>
              <AnimatePresence mode="wait">
                <motion.div
                  key={activeTab}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.12 }}
                  style={{ height: '100%' }}
                >
                  <ActivityPanel tab={activeTab} />
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </main>
      </div>

      <Toast />

      {showRegister && <RegisterAgentModal onClose={() => setShowRegister(false)} />}
    </div>
  )
}
