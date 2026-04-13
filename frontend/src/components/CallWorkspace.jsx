import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMarket } from '../context/MarketContext'
import { callAgent } from '../api'

// ── Signal badge ─────────────────────────────────────────────────────────────
function SignalBadge({ signal }) {
  const map = {
    positive: { bg: 'var(--positive-bg)', border: 'var(--positive-border)',
      color: 'var(--positive)', icon: '↑', label: 'Positive' },
    neutral:  { bg: 'var(--neutral-bg)',   border: 'var(--neutral-border)',
      color: 'var(--neutral-color)', icon: '→', label: 'Neutral' },
    negative: { bg: 'var(--negative-bg)', border: 'var(--negative-border)',
      color: 'var(--negative)', icon: '↓', label: 'Negative' },
  }
  const s = map[signal] ?? map.neutral
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '4px 12px', borderRadius: 20,
      background: s.bg, border: `1px solid ${s.border}`, color: s.color,
      fontSize: 13, fontWeight: 600,
    }}>
      {s.icon} {s.label}
    </span>
  )
}

// ── Brief result ─────────────────────────────────────────────────────────────
function BriefCard({ brief }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, ease: [0.25, 0.1, 0.25, 1] }}
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        overflow: 'hidden',
        boxShadow: 'var(--shadow-sm)',
        marginTop: 20,
      }}
    >
      {/* Header */}
      <div style={{
        padding: '18px 24px',
        borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              <span style={{
                fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 22,
                color: 'var(--text-primary)',
              }}>
                {brief.ticker}
              </span>
              <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
                {brief.company_name}
              </span>
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
              {brief.filing_type} · Filed {brief.filing_date}
            </div>
          </div>
        </div>
        <SignalBadge signal={brief.signal} />
      </div>

      <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>
        {/* Signal reasoning */}
        <div style={{
          padding: '14px 16px',
          borderRadius: 'var(--radius-md)',
          background: brief.signal === 'positive' ? 'var(--positive-bg)' :
            brief.signal === 'negative' ? 'var(--negative-bg)' : 'var(--neutral-bg)',
          border: `1px solid ${brief.signal === 'positive' ? 'var(--positive-border)' :
            brief.signal === 'negative' ? 'var(--negative-border)' : 'var(--neutral-border)'}`,
          fontSize: 14, lineHeight: 1.6,
          color: brief.signal === 'positive' ? 'var(--positive)' :
            brief.signal === 'negative' ? 'var(--negative)' : 'var(--neutral-color)',
        }}>
          {brief.signal_reasoning}
        </div>

        {/* Summary */}
        <div>
          <h4 style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
            Business summary
          </h4>
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
            {brief.business_summary}
          </p>
        </div>

        {/* Two-column: highlights + risks */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <div>
            <h4 style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
              Financial highlights
            </h4>
            <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(brief.recent_financial_highlights ?? []).map((h, i) => (
                <motion.li
                  key={i}
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.1 + i * 0.05 }}
                  style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 13,
                    color: 'var(--text-secondary)', lineHeight: 1.5 }}
                >
                  <span style={{ color: 'var(--positive)', flexShrink: 0, marginTop: 2, fontSize: 10 }}>●</span>
                  {h}
                </motion.li>
              ))}
            </ul>
          </div>

          <div>
            <h4 style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
              Key risks
            </h4>
            <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {(brief.key_risks ?? []).map((r, i) => (
                <motion.li
                  key={i}
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.15 + i * 0.05 }}
                  style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 13,
                    color: 'var(--text-secondary)', lineHeight: 1.5 }}
                >
                  <span style={{ color: 'var(--negative)', flexShrink: 0, marginTop: 2, fontSize: 10 }}>●</span>
                  {r}
                </motion.li>
              ))}
            </ul>
          </div>
        </div>

        <div style={{ fontSize: 11, color: 'var(--text-muted)', textAlign: 'right' }}>
          Generated {new Date(brief.generated_at).toLocaleString()}
        </div>
      </div>
    </motion.div>
  )
}

// ── Loading skeleton ──────────────────────────────────────────────────────────
function LoadingSkeleton() {
  const bar = (w, h = 14) => (
    <div style={{
      width: w, height: h, borderRadius: 4,
      background: 'var(--border)',
      animation: 'pulse 1.4s ease-in-out infinite',
    }} />
  )
  return (
    <motion.div
      initial={{ opacity: 0 }} animate={{ opacity: 1 }}
      style={{
        marginTop: 20, background: 'var(--surface)',
        border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)',
        padding: '24px', display: 'flex', flexDirection: 'column', gap: 16,
      }}
    >
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.45} }`}</style>
      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
        {bar('30%', 22)}
        {bar(80, 24)}
      </div>
      {bar('100%', 52)}
      {bar('90%')}
      {bar('75%')}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {[90, 80, 85, 60].map(w => bar(`${w}%`))}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {[70, 85, 75, 65].map(w => bar(`${w}%`))}
        </div>
      </div>
    </motion.div>
  )
}

// ── Main workspace ─────────────────────────────────────────────────────────────
export default function CallWorkspace({ agent }) {
  const { apiKey, showToast, refreshWallet, refresh } = useMarket()
  const [ticker, setTicker]   = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult]   = useState(null)   // { brief } | { error, status }
  const [lastAgent, setLastAgent] = useState(null)

  // Reset result if agent changes
  if (agent?.agent_id !== lastAgent) {
    setLastAgent(agent?.agent_id)
    setResult(null)
  }

  const submit = async (e) => {
    e?.preventDefault()
    const t = ticker.trim().toUpperCase()
    if (!t || loading) return
    setLoading(true)
    setResult(null)
    try {
      const { status, ok, body } = await callAgent(apiKey, agent.agent_id, { ticker: t })
      if (ok) {
        setResult({ brief: body })
        showToast(`${t} — ${body.signal} signal`, 'success')
      } else if (status === 402) {
        const d = body.detail ?? {}
        setResult({ error: `Insufficient balance. You have ${d.balance_cents ?? 0}¢, this call costs ${d.required_cents ?? 1}¢.`, status })
        showToast('Not enough balance — add funds', 'error')
      } else {
        const msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
        setResult({ error: msg, status })
        showToast(`Call failed (${status})`, 'error')
      }
    } catch (err) {
      setResult({ error: err.message, status: 0 })
      showToast('Network error', 'error')
    } finally {
      setLoading(false)
      setTimeout(() => { refreshWallet(); refresh() }, 500)
    }
  }

  return (
    <div style={{ maxWidth: 720, paddingBottom: 28 }}>
      {/* Agent header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <h1 style={{ fontSize: 20, fontWeight: 700, letterSpacing: '-0.02em',
              marginBottom: 4, color: 'var(--text-primary)' }}>
              {agent.name}
            </h1>
            <p style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
              {agent.description}
            </p>
          </div>
          <div style={{ textAlign: 'right', flexShrink: 0, marginLeft: 24 }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 20,
              color: 'var(--text-primary)' }}>
              ${agent.price_per_call_usd.toFixed(2)}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>per call</div>
          </div>
        </div>

        {/* Mini stats */}
        <div style={{ display: 'flex', gap: 20, marginTop: 14 }}>
          {[
            { label: 'Total calls', value: agent.total_calls },
            { label: 'Success rate', value: agent.total_calls > 0 ? `${Math.round(agent.success_rate * 100)}%` : '—' },
            { label: 'Avg latency', value: agent.avg_latency_ms > 0 ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—' },
            { label: 'Tags', value: agent.tags.join(', ') || '—' },
          ].map(s => (
            <div key={s.label}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 500,
                textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 2 }}>
                {s.label}
              </div>
              <div style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-primary)',
                fontFamily: typeof s.value === 'number' ? 'var(--font-mono)' : 'var(--font-sans)' }}>
                {s.value}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Call form */}
      <form
        onSubmit={submit}
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '20px 20px',
          boxShadow: 'var(--shadow-sm)',
        }}
      >
        <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)',
          display: 'block', marginBottom: 8 }}>
          Ticker symbol
        </label>
        <div style={{ display: 'flex', gap: 10 }}>
          <div style={{
            flex: 1, position: 'relative',
            display: 'flex', alignItems: 'center',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-md)',
            background: 'var(--bg)',
            overflow: 'hidden',
            boxShadow: 'var(--shadow-xs)',
          }}>
            <span style={{ padding: '0 12px', fontSize: 14, color: 'var(--text-muted)',
              borderRight: '1px solid var(--border)', flexShrink: 0 }}>$</span>
            <input
              type="text"
              maxLength={5}
              placeholder="AAPL"
              value={ticker}
              onChange={e => setTicker(e.target.value.toUpperCase())}
              disabled={loading}
              style={{
                flex: 1, padding: '10px 14px',
                background: 'transparent', border: 'none',
                fontSize: 16, fontFamily: 'var(--font-mono)', fontWeight: 600,
                color: 'var(--text-primary)', letterSpacing: '0.04em',
                outline: 'none',
              }}
            />
          </div>
          <button
            type="submit"
            disabled={loading || !ticker.trim()}
            style={{
              padding: '10px 24px', borderRadius: 'var(--radius-md)',
              background: loading || !ticker.trim() ? 'var(--border)' : 'var(--brand)',
              color: loading || !ticker.trim() ? 'var(--text-muted)' : 'white',
              fontSize: 14, fontWeight: 500,
              cursor: loading || !ticker.trim() ? 'not-allowed' : 'pointer',
              transition: 'background 0.15s, color 0.15s',
              minWidth: 100,
            }}
          >
            {loading ? 'Fetching…' : 'Analyze →'}
          </button>
        </div>
        <p style={{ marginTop: 8, fontSize: 12, color: 'var(--text-muted)' }}>
          Fetches the latest SEC 10-K or 10-Q and runs LLM synthesis · takes ~20–40s
        </p>
      </form>

      {/* Result area */}
      <AnimatePresence mode="wait">
        {loading && <LoadingSkeleton key="loading" />}
        {!loading && result?.error && (
          <motion.div
            key="error"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            style={{
              marginTop: 20, padding: '16px 20px',
              background: 'var(--negative-bg)', border: '1px solid var(--negative-border)',
              borderRadius: 'var(--radius-lg)', fontSize: 14,
              color: 'var(--negative)', lineHeight: 1.5,
            }}
          >
            <strong>Error {result.status > 0 ? `(${result.status})` : ''}:</strong> {result.error}
          </motion.div>
        )}
        {!loading && result?.brief && (
          <BriefCard key={result.brief.ticker + result.brief.generated_at} brief={result.brief} />
        )}
      </AnimatePresence>
    </div>
  )
}
