import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMarket } from '../context/MarketContext'
import { callAgent, depositToWallet } from '../api'
import CallWorkspace from './CallWorkspace'
import ActivityPanel from './ActivityPanel'

// ── Helpers ──────────────────────────────────────────────────────────────────

function StatusDot({ color = 'var(--positive)' }) {
  return (
    <span style={{ width: 7, height: 7, borderRadius: '50%',
      background: color, display: 'inline-block', flexShrink: 0 }} />
  )
}

// ── Wallet widget ─────────────────────────────────────────────────────────────

function WalletWidget() {
  const { apiKey, wallet, refreshWallet, showToast } = useMarket()
  const [amount, setAmount] = useState('')
  const [open, setOpen] = useState(false)
  const [working, setWorking] = useState(false)

  const deposit = async (e) => {
    e?.preventDefault()
    const cents = parseInt(amount, 10)
    if (!cents || cents <= 0) return
    setWorking(true)
    try {
      await depositToWallet(apiKey, wallet.wallet_id, cents)
      await refreshWallet()
      showToast(`Added ${cents}¢ to your wallet`, 'success')
      setAmount(''); setOpen(false)
    } catch (err) {
      showToast(err.message, 'error')
    } finally { setWorking(false) }
  }

  if (!wallet) return null

  const isLow = wallet.balance_cents < 5

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '6px 14px', borderRadius: 'var(--radius-md)',
          background: 'var(--surface)',
          border: `1px solid ${isLow ? 'var(--neutral-border)' : 'var(--border)'}`,
          boxShadow: 'var(--shadow-xs)',
          fontSize: 14, cursor: 'pointer',
        }}
      >
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
          stroke={isLow ? 'var(--neutral-color)' : 'var(--text-muted)'} strokeWidth="2">
          <rect x="2" y="5" width="20" height="14" rx="2"/>
          <path d="M16 12h.01"/>
        </svg>
        <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 700,
          color: isLow ? 'var(--neutral-color)' : 'var(--text-primary)', fontSize: 14 }}>
          {wallet.balance_cents}¢
        </span>
        <span style={{ color: 'var(--brand)', fontSize: 12, fontWeight: 500 }}>+ Add</span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.15 }}
            style={{
              position: 'absolute', top: 'calc(100% + 8px)', right: 0,
              background: 'var(--surface)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-lg)', boxShadow: 'var(--shadow-lg)',
              padding: 20, minWidth: 280, zIndex: 50,
            }}
          >
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 2 }}>
                Wallet balance
              </div>
              <div style={{ fontSize: 26, fontFamily: 'var(--font-mono)', fontWeight: 700,
                color: 'var(--text-primary)' }}>
                {wallet.balance_cents}<span style={{ fontSize: 16, color: 'var(--text-muted)' }}>¢</span>
              </div>
              {isLow && (
                <div style={{ fontSize: 12, color: 'var(--neutral-color)', marginTop: 4 }}>
                  Low balance — add funds to keep calling agents
                </div>
              )}
            </div>
            <form onSubmit={deposit} style={{ display: 'flex', gap: 8 }}>
              <input
                autoFocus
                type="number"
                min={1}
                placeholder="Amount in cents (e.g. 500)"
                value={amount}
                onChange={e => setAmount(e.target.value)}
                style={{
                  flex: 1, padding: '8px 12px',
                  border: '1px solid var(--border)',
                  borderRadius: 'var(--radius-md)',
                  fontSize: 13, background: 'var(--bg)',
                  outline: 'none',
                }}
              />
              <button
                type="submit"
                disabled={working || !amount}
                style={{
                  padding: '8px 16px', borderRadius: 'var(--radius-md)',
                  background: 'var(--brand)', color: 'white',
                  fontSize: 13, fontWeight: 600,
                  opacity: working || !amount ? 0.45 : 1,
                  cursor: working || !amount ? 'not-allowed' : 'pointer',
                  whiteSpace: 'nowrap',
                }}
              >
                {working ? '…' : 'Add funds'}
              </button>
            </form>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Agent card in sidebar ─────────────────────────────────────────────────────

function AgentCard({ agent, selected, onClick }) {
  const successRate = agent.total_calls > 0
    ? Math.round(agent.success_rate * 100) : null
  const statusColor = successRate === null ? 'var(--text-muted)' :
    successRate >= 90 ? 'var(--positive)' :
    successRate >= 60 ? 'var(--neutral-color)' : 'var(--negative)'

  return (
    <button
      onClick={() => onClick(agent)}
      style={{
        width: '100%', textAlign: 'left',
        padding: '14px 16px',
        borderRadius: 'var(--radius-md)',
        border: `1px solid ${selected ? 'var(--brand-border)' : 'var(--border)'}`,
        background: selected ? 'var(--brand-light)' : 'var(--surface)',
        cursor: 'pointer',
        transition: 'border-color 0.12s, background 0.12s',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between',
        alignItems: 'flex-start', marginBottom: 6 }}>
        <span style={{ fontWeight: 600, fontSize: 13,
          color: selected ? 'var(--brand)' : 'var(--text-primary)', lineHeight: 1.35 }}>
          {agent.name}
        </span>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 700,
          color: selected ? 'var(--brand)' : 'var(--text-primary)',
          flexShrink: 0, marginLeft: 8,
          background: selected ? 'rgba(92,80,232,0.1)' : 'var(--surface-subtle)',
          padding: '2px 7px', borderRadius: 4,
          border: '1px solid var(--border)',
        }}>
          ${agent.price_per_call_usd.toFixed(2)}
        </span>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-muted)',
        lineHeight: 1.5, marginBottom: 10,
        display: '-webkit-box', WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>
        {agent.description}
      </p>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
          <StatusDot color={statusColor} />
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {successRate !== null ? `${successRate}% success` : 'New'}
          </span>
        </div>
        {agent.avg_latency_ms > 0 && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {(agent.avg_latency_ms / 1000).toFixed(1)}s avg
          </span>
        )}
        <span style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 'auto' }}>
          {agent.total_calls} call{agent.total_calls !== 1 ? 's' : ''}
        </span>
      </div>
    </button>
  )
}

// ── Toast ─────────────────────────────────────────────────────────────────────

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
            position: 'fixed', top: 68, left: '50%',
            background: s.bg, border: `1px solid ${s.border}`,
            borderRadius: 'var(--radius-md)', color: s.text,
            fontSize: 13, fontWeight: 500,
            padding: '10px 18px',
            boxShadow: 'var(--shadow-md)',
            zIndex: 100, pointerEvents: 'none',
            maxWidth: 380, textAlign: 'center',
          }}
        >
          {toast.msg}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

// ── User avatar ───────────────────────────────────────────────────────────────

function UserBadge({ user, onSignOut }) {
  const [open, setOpen] = useState(false)
  const initial = (user?.username ?? 'A')[0].toUpperCase()

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: 32, height: 32, borderRadius: '50%',
          background: 'var(--brand)', color: 'white',
          fontWeight: 700, fontSize: 13,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          cursor: 'pointer', border: '2px solid var(--brand-border)',
          flexShrink: 0,
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
              background: 'var(--surface)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-lg)', boxShadow: 'var(--shadow-lg)',
              padding: '4px', minWidth: 200, zIndex: 60,
            }}
          >
            <div style={{ padding: '10px 14px', borderBottom: '1px solid var(--border)' }}>
              <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                {user?.username ?? 'Agent'}
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 1 }}>
                {user?.email ?? ''}
              </div>
            </div>
            <button
              onClick={() => { setOpen(false); onSignOut() }}
              style={{
                width: '100%', padding: '9px 14px', textAlign: 'left',
                fontSize: 13, color: 'var(--negative)', cursor: 'pointer',
                borderRadius: 6,
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

// ── Main dashboard ────────────────────────────────────────────────────────────

export default function Dashboard({ onSignOut, user }) {
  const { agents, loading } = useMarket()
  const [selectedAgent, setSelectedAgent] = useState(null)
  const [activeTab, setActiveTab] = useState('activity')

  const agent = selectedAgent ?? agents[0] ?? null

  return (
    <div style={{ height: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--bg)' }}>
      {/* Header */}
      <header style={{
        height: 'var(--header-h)', flexShrink: 0,
        background: 'var(--surface)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 20px',
        position: 'sticky', top: 0, zIndex: 20,
        boxShadow: 'var(--shadow-xs)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              width: 24, height: 24, borderRadius: 5,
              background: 'var(--brand)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
                <circle cx="12" cy="12" r="3"/>
                <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
              </svg>
            </div>
            <span style={{ fontWeight: 700, fontSize: 14, letterSpacing: '-0.01em' }}>
              agentmarket
            </span>
          </div>

          {/* Online indicator */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 5,
            padding: '3px 10px', borderRadius: 20,
            background: 'var(--positive-bg)', border: '1px solid var(--positive-border)',
          }}>
            <StatusDot />
            <span style={{ fontSize: 11, color: 'var(--positive)', fontWeight: 600 }}>
              {agents.length} agent{agents.length !== 1 ? 's' : ''} available
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <WalletWidget />
          <UserBadge user={user} onSignOut={onSignOut} />
        </div>
      </header>

      {/* Body */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0 }}>

        {/* Sidebar — registry */}
        <aside style={{
          width: 'var(--sidebar-w)', flexShrink: 0,
          borderRight: '1px solid var(--border)',
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
          background: 'var(--surface)',
        }}>
          <div style={{
            padding: '14px 16px 10px',
            borderBottom: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <div>
              <h2 style={{ fontSize: 12, fontWeight: 700, color: 'var(--text-muted)',
                textTransform: 'uppercase', letterSpacing: '0.08em' }}>
                Agent Registry
              </h2>
              <p style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
                Select an agent to call
              </p>
            </div>
          </div>

          <div style={{ flex: 1, overflowY: 'auto', padding: '10px 10px',
            display: 'flex', flexDirection: 'column', gap: 6 }}>
            {loading ? (
              <div style={{ padding: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
                {[1, 2].map(i => (
                  <div key={i} style={{ height: 90, borderRadius: 8,
                    background: 'var(--border)', animation: 'pulse 1.4s infinite' }} />
                ))}
                <style>{`@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}`}</style>
              </div>
            ) : agents.length === 0 ? (
              <div style={{ padding: '20px 8px', textAlign: 'center' }}>
                <div style={{ fontSize: 13, color: 'var(--text-muted)', lineHeight: 1.6 }}>
                  No agents registered yet.
                </div>
              </div>
            ) : agents.map(a => (
              <motion.div
                key={a.agent_id}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.25 }}
              >
                <AgentCard
                  agent={a}
                  selected={agent?.agent_id === a.agent_id}
                  onClick={setSelectedAgent}
                />
              </motion.div>
            ))}
          </div>

          {/* Register agent CTA */}
          <div style={{
            padding: '12px 14px',
            borderTop: '1px solid var(--border)',
            background: 'var(--surface-subtle)',
          }}>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.5 }}>
              Have a specialized agent?{' '}
              <span style={{ color: 'var(--brand)', fontWeight: 500 }}>List it</span> to earn 90% per call.
            </div>
            <a
              href="https://github.com/AnayGarodia/agentmarket#registry"
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: 'block', textAlign: 'center',
                padding: '7px 0', borderRadius: 'var(--radius-md)',
                border: '1px solid var(--brand-border)',
                background: 'var(--brand-light)',
                color: 'var(--brand)', fontSize: 12, fontWeight: 600,
              }}
            >
              Register an agent →
            </a>
          </div>
        </aside>

        {/* Main content */}
        <main style={{ flex: 1, display: 'flex', flexDirection: 'column',
          overflow: 'hidden', minWidth: 0 }}>

          {/* Call workspace */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '24px 28px 0' }}>
            {agent ? (
              <CallWorkspace agent={agent} />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center',
                height: '100%', gap: 12, textAlign: 'center' }}>
                <div style={{ fontSize: 32 }}>🤖</div>
                <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--text-primary)' }}>
                  Select an agent from the registry
                </div>
                <div style={{ fontSize: 13, color: 'var(--text-muted)', maxWidth: 300 }}>
                  Browse the listed agents on the left and click one to start calling it.
                </div>
              </div>
            )}
          </div>

          {/* Bottom panel */}
          <div style={{
            flexShrink: 0,
            borderTop: '1px solid var(--border)',
            background: 'var(--surface)',
          }}>
            <div style={{
              display: 'flex', borderBottom: '1px solid var(--border)',
              padding: '0 20px',
            }}>
              {[
                { id: 'activity',  label: 'Transactions' },
                { id: 'analytics', label: 'Analytics' },
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  style={{
                    padding: '10px 16px', fontSize: 13, fontWeight: 500,
                    color: activeTab === tab.id ? 'var(--brand)' : 'var(--text-muted)',
                    borderBottom: `2px solid ${activeTab === tab.id ? 'var(--brand)' : 'transparent'}`,
                    cursor: 'pointer', transition: 'color 0.15s',
                    marginBottom: -1,
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            <div style={{ height: 240, overflowY: 'auto' }}>
              <AnimatePresence mode="wait">
                <motion.div
                  key={activeTab}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.13 }}
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
    </div>
  )
}
