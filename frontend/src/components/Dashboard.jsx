import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMarket } from '../context/MarketContext'
import { callAgent, depositToWallet } from '../api'
import CallWorkspace from './CallWorkspace'
import ActivityPanel from './ActivityPanel'

// ── Small reusable pieces ────────────────────────────────────────────────────

function StatusDot({ color = 'var(--positive)' }) {
  return (
    <span style={{ width: 7, height: 7, borderRadius: '50%',
      background: color, display: 'inline-block', flexShrink: 0 }} />
  )
}

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

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '6px 14px', borderRadius: 'var(--radius-md)',
          background: open ? 'var(--surface)' : 'var(--surface)',
          border: '1px solid var(--border)',
          boxShadow: 'var(--shadow-xs)',
          fontSize: 14, cursor: 'pointer',
        }}
      >
        <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>Balance</span>
        <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600,
          color: 'var(--text-primary)', fontSize: 15 }}>
          {wallet.balance_cents}¢
        </span>
        <span style={{ color: 'var(--brand)', fontSize: 12, fontWeight: 500 }}>+ Add funds</span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.15 }}
            style={{
              position: 'absolute', top: 'calc(100% + 6px)', right: 0,
              background: 'var(--surface)', border: '1px solid var(--border)',
              borderRadius: 'var(--radius-lg)', boxShadow: 'var(--shadow-lg)',
              padding: 16, minWidth: 260, zIndex: 50,
            }}
          >
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>
              Add cents to your wallet. 1 call costs {1}¢.
            </p>
            <form onSubmit={deposit} style={{ display: 'flex', gap: 8 }}>
              <input
                autoFocus
                type="number"
                min={1}
                placeholder="e.g. 500"
                value={amount}
                onChange={e => setAmount(e.target.value)}
                style={{
                  flex: 1, padding: '8px 12px',
                  border: '1px solid var(--border)',
                  borderRadius: 'var(--radius-md)',
                  fontSize: 14, background: 'var(--bg)',
                  outline: 'none',
                }}
              />
              <button
                type="submit"
                disabled={working || !amount}
                style={{
                  padding: '8px 16px', borderRadius: 'var(--radius-md)',
                  background: 'var(--brand)', color: 'white',
                  fontSize: 14, fontWeight: 500,
                  opacity: working || !amount ? 0.5 : 1,
                  cursor: working || !amount ? 'not-allowed' : 'pointer',
                }}
              >
                {working ? '…' : 'Add'}
              </button>
            </form>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

// ── Agent card in sidebar ────────────────────────────────────────────────────

function AgentSidebarCard({ agent, selected, onClick }) {
  const successRate = agent.total_calls > 0
    ? Math.round(agent.success_rate * 100) : null

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
        transition: 'all 0.12s',
        boxShadow: selected ? 'none' : 'var(--shadow-xs)',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between',
        alignItems: 'flex-start', marginBottom: 6 }}>
        <span style={{ fontWeight: 600, fontSize: 14,
          color: selected ? 'var(--brand)' : 'var(--text-primary)', lineHeight: 1.3 }}>
          {agent.name}
        </span>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 600,
          color: selected ? 'var(--brand)' : 'var(--text-primary)',
          flexShrink: 0, marginLeft: 8,
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
          <StatusDot color={successRate === null ? 'var(--brand)' :
            successRate >= 90 ? 'var(--positive)' :
            successRate >= 60 ? 'var(--neutral-color)' : 'var(--negative)'} />
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {successRate !== null ? `${successRate}% success` : 'New'}
          </span>
        </div>
        {agent.avg_latency_ms > 0 && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            {Math.round(agent.avg_latency_ms / 1000)}s avg
          </span>
        )}
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {agent.total_calls} call{agent.total_calls !== 1 ? 's' : ''}
        </span>
      </div>
    </button>
  )
}

// ── Toast ────────────────────────────────────────────────────────────────────

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
          transition={{ duration: 0.2, ease: [0.25, 0.1, 0.25, 1] }}
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

// ── Main dashboard ────────────────────────────────────────────────────────────

export default function Dashboard({ onSignOut }) {
  const { agents, wallet, apiKey, showToast, refresh } = useMarket()
  const [selectedAgent, setSelectedAgent] = useState(null)
  const [activeTab, setActiveTab] = useState('activity') // 'activity' | 'analytics'

  // Pick the first agent by default once loaded
  const agent = selectedAgent ?? agents[0] ?? null

  return (
    <div style={{ minHeight: '100vh', display: 'flex', flexDirection: 'column', background: 'var(--bg)' }}>
      {/* Header */}
      <header style={{
        height: 'var(--header-h)', flexShrink: 0,
        background: 'var(--surface)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0 24px',
        position: 'sticky', top: 0, zIndex: 20,
        boxShadow: 'var(--shadow-xs)',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
          <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.01em' }}>
            agentmarket
          </span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6,
            padding: '4px 10px', borderRadius: 'var(--radius-sm)',
            background: 'var(--positive-bg)', border: '1px solid var(--positive-border)' }}>
            <StatusDot />
            <span style={{ fontSize: 12, color: 'var(--positive)', fontWeight: 500 }}>
              {agents.length} agent{agents.length !== 1 ? 's' : ''} online
            </span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <WalletWidget />
          <button
            onClick={onSignOut}
            style={{
              fontSize: 13, color: 'var(--text-muted)', padding: '6px 10px',
              borderRadius: 'var(--radius-sm)', cursor: 'pointer',
            }}
          >
            Sign out
          </button>
        </div>
      </header>

      {/* Body */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0 }}>

        {/* Left sidebar — Agent registry */}
        <aside style={{
          width: 'var(--sidebar-w)', flexShrink: 0,
          borderRight: '1px solid var(--border)',
          display: 'flex', flexDirection: 'column',
          overflow: 'hidden',
          background: 'var(--surface)',
        }}>
          <div style={{
            padding: '16px 16px 12px',
            borderBottom: '1px solid var(--border)',
          }}>
            <h2 style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.06em' }}>
              Registry
            </h2>
          </div>

          <div style={{ flex: 1, overflowY: 'auto', padding: 12,
            display: 'flex', flexDirection: 'column', gap: 8 }}>
            {agents.length === 0 ? (
              <p style={{ fontSize: 13, color: 'var(--text-muted)', padding: '12px 4px' }}>
                No agents registered yet.
              </p>
            ) : agents.map(a => (
              <motion.div
                key={a.agent_id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.3 }}
              >
                <AgentSidebarCard
                  agent={a}
                  selected={agent?.agent_id === a.agent_id}
                  onClick={setSelectedAgent}
                />
              </motion.div>
            ))}
          </div>
        </aside>

        {/* Main content */}
        <main style={{ flex: 1, display: 'flex', flexDirection: 'column',
          overflow: 'hidden', minWidth: 0 }}>

          {/* Top area — call workspace */}
          <div style={{ flex: 1, overflowY: 'auto', padding: '28px 28px 0' }}>
            {agent ? (
              <CallWorkspace agent={agent} />
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                height: '100%', color: 'var(--text-muted)', fontSize: 14 }}>
                Select an agent to get started
              </div>
            )}
          </div>

          {/* Bottom tabs — activity & analytics */}
          <div style={{
            flexShrink: 0,
            borderTop: '1px solid var(--border)',
            background: 'var(--surface)',
          }}>
            {/* Tab bar */}
            <div style={{
              display: 'flex', gap: 0,
              borderBottom: '1px solid var(--border)',
              padding: '0 20px',
            }}>
              {[
                { id: 'activity',  label: 'Activity' },
                { id: 'analytics', label: 'Analytics' },
              ].map(tab => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  style={{
                    padding: '10px 16px',
                    fontSize: 13, fontWeight: 500,
                    color: activeTab === tab.id ? 'var(--brand)' : 'var(--text-muted)',
                    borderBottom: `2px solid ${activeTab === tab.id ? 'var(--brand)' : 'transparent'}`,
                    cursor: 'pointer',
                    transition: 'color 0.15s',
                    marginBottom: -1,
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {/* Tab content */}
            <div style={{ height: 240, overflowY: 'auto' }}>
              <AnimatePresence mode="wait">
                <motion.div
                  key={activeTab}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.15 }}
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
