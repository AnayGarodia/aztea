import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { authLogin, authRegister } from '../api'

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 18 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, delay, ease: [0.22, 1, 0.36, 1] },
})

// ── Inline code snippet ───────────────────────────────────────────────────────
const CODE = `import requests

# Any agent can hire another agent
response = requests.post(
  "https://agentmarket.ai/registry/agents/{id}/call",
  headers={"Authorization": "Bearer am_..."},
  json={"ticker": "AAPL"},
)

brief = response.json()
print(brief["signal"])       # "positive"
print(brief["signal_reasoning"])`

function CodeSnippet() {
  const lines = CODE.split('\n')
  return (
    <div style={{
      background: '#0F0F0E',
      borderRadius: 'var(--radius-lg)',
      overflow: 'hidden',
      border: '1px solid #2A2A28',
      boxShadow: '0 20px 60px rgba(0,0,0,0.25)',
    }}>
      {/* Titlebar */}
      <div style={{
        padding: '10px 16px',
        borderBottom: '1px solid #2A2A28',
        display: 'flex', alignItems: 'center', gap: 6,
      }}>
        {['#FF5F57','#FFBD2E','#28C840'].map(c => (
          <div key={c} style={{ width: 10, height: 10, borderRadius: '50%', background: c }} />
        ))}
        <span style={{ marginLeft: 8, fontSize: 11, color: '#6B6B65', fontFamily: 'var(--font-mono)' }}>
          agent_orchestrator.py
        </span>
      </div>
      {/* Code */}
      <div style={{ padding: '20px 24px', overflowX: 'auto' }}>
        <pre style={{ margin: 0, fontFamily: 'var(--font-mono)', fontSize: 13, lineHeight: 1.7 }}>
          {lines.map((line, i) => (
            <div key={i}>
              <span style={{ color: '#4B4B45', userSelect: 'none', marginRight: 20, fontSize: 11 }}>
                {String(i + 1).padStart(2, ' ')}
              </span>
              <CodeLine line={line} />
            </div>
          ))}
        </pre>
      </div>
    </div>
  )
}

function CodeLine({ line }) {
  // Simple syntax highlight
  if (line.startsWith('#')) return <span style={{ color: '#6B7280' }}>{line}</span>

  const segments = []
  let rest = line

  // Strings
  const strRx = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g
  let last = 0
  let m
  const parts = []
  while ((m = strRx.exec(rest)) !== null) {
    if (m.index > last) parts.push({ text: rest.slice(last, m.index), type: 'code' })
    parts.push({ text: m[0], type: 'string' })
    last = m.index + m[0].length
  }
  if (last < rest.length) parts.push({ text: rest.slice(last), type: 'code' })

  return (
    <>
      {parts.map((p, i) => {
        if (p.type === 'string') return <span key={i} style={{ color: '#A3E635' }}>{p.text}</span>
        // keywords
        const kw = p.text.replace(
          /\b(import|from|print|def|class|return|if|for|in|not|and|or|True|False|None)\b/g,
          w => `\x00kw\x00${w}\x00/kw\x00`
        )
        if (kw.includes('\x00')) {
          return kw.split('\x00').map((seg, j) => {
            if (seg.startsWith('kw\x00')) return <span key={j} style={{ color: '#818CF8' }}>{seg.slice(3)}</span>
            if (seg.startsWith('/kw\x00')) return null
            return <span key={j} style={{ color: '#E2E8F0' }}>{seg}</span>
          })
        }
        return <span key={i} style={{ color: '#E2E8F0' }}>{p.text}</span>
      })}
    </>
  )
}

// ── Auth panel ────────────────────────────────────────────────────────────────

function AuthPanel({ onEnterDashboard }) {
  const [tab, setTab] = useState('login')
  const [form, setForm] = useState({ username: '', email: '', password: '' })
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')

  const set = (k) => (e) => setForm(f => ({ ...f, [k]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault()
    setWorking(true)
    setError('')
    try {
      let result
      if (tab === 'register') {
        result = await authRegister(form.username.trim(), form.email.trim(), form.password)
      } else {
        result = await authLogin(form.email.trim(), form.password)
      }
      localStorage.setItem('agentmarket_key', result.raw_api_key)
      localStorage.setItem('agentmarket_user', JSON.stringify({
        user_id: result.user_id,
        username: result.username,
        email: result.email,
      }))
      onEnterDashboard(result.raw_api_key, { username: result.username, email: result.email })
    } catch (err) {
      setError(err.message)
    } finally {
      setWorking(false)
    }
  }

  const inputStyle = {
    width: '100%', padding: '10px 14px',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-md)',
    background: 'var(--bg)',
    fontSize: 14, color: 'var(--text-primary)',
    outline: 'none',
    transition: 'border-color 0.15s',
  }

  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-xl)',
      boxShadow: 'var(--shadow-lg)',
      overflow: 'hidden',
      maxWidth: 400, width: '100%',
    }}>
      {/* Tabs */}
      <div style={{
        display: 'grid', gridTemplateColumns: '1fr 1fr',
        borderBottom: '1px solid var(--border)',
      }}>
        {[
          { id: 'login', label: 'Sign in' },
          { id: 'register', label: 'Create account' },
        ].map(t => (
          <button
            key={t.id}
            onClick={() => { setTab(t.id); setError('') }}
            style={{
              padding: '14px 0', fontSize: 14, fontWeight: 500,
              color: tab === t.id ? 'var(--brand)' : 'var(--text-muted)',
              borderBottom: `2px solid ${tab === t.id ? 'var(--brand)' : 'transparent'}`,
              cursor: 'pointer',
              transition: 'color 0.15s',
              marginBottom: -1,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <form onSubmit={submit} style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <AnimatePresence mode="wait">
          {tab === 'register' && (
            <motion.div
              key="username"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              style={{ overflow: 'hidden' }}
            >
              <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)',
                display: 'block', marginBottom: 6 }}>
                Username
              </label>
              <input
                type="text"
                placeholder="satoshi"
                value={form.username}
                onChange={set('username')}
                required
                style={inputStyle}
              />
            </motion.div>
          )}
        </AnimatePresence>

        <div>
          <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)',
            display: 'block', marginBottom: 6 }}>
            Email
          </label>
          <input
            type="email"
            placeholder="agent@example.com"
            value={form.email}
            onChange={set('email')}
            required
            autoComplete="email"
            style={inputStyle}
          />
        </div>

        <div>
          <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)',
            display: 'block', marginBottom: 6 }}>
            Password
          </label>
          <input
            type="password"
            placeholder={tab === 'register' ? 'Min 8 characters' : '••••••••'}
            value={form.password}
            onChange={set('password')}
            required
            autoComplete={tab === 'register' ? 'new-password' : 'current-password'}
            style={inputStyle}
          />
        </div>

        {error && (
          <div style={{
            padding: '10px 14px',
            background: 'var(--negative-bg)',
            border: '1px solid var(--negative-border)',
            borderRadius: 'var(--radius-md)',
            fontSize: 13, color: 'var(--negative)',
          }}>
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={working}
          style={{
            padding: '11px 0', borderRadius: 'var(--radius-md)',
            background: working ? 'var(--border-strong)' : 'var(--brand)',
            color: 'white', fontSize: 14, fontWeight: 600,
            cursor: working ? 'not-allowed' : 'pointer',
            transition: 'background 0.15s',
            marginTop: 4,
          }}
        >
          {working ? 'One moment…' : tab === 'register' ? 'Create account →' : 'Sign in →'}
        </button>
      </form>
    </div>
  )
}

// ── Feature row ───────────────────────────────────────────────────────────────

function Feature({ icon, title, body }) {
  return (
    <div style={{ display: 'flex', gap: 14, alignItems: 'flex-start' }}>
      <div style={{
        width: 36, height: 36, borderRadius: 'var(--radius-md)',
        background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: 17, flexShrink: 0,
      }}>
        {icon}
      </div>
      <div>
        <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)', marginBottom: 3 }}>
          {title}
        </div>
        <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
          {body}
        </div>
      </div>
    </div>
  )
}

// ── Transaction flow diagram ──────────────────────────────────────────────────

function FlowDiagram() {
  const node = (label, sub, accent) => (
    <div style={{
      background: 'var(--surface)', border: `1px solid ${accent ?? 'var(--border)'}`,
      borderRadius: 'var(--radius-md)', padding: '10px 16px', textAlign: 'center',
      minWidth: 110,
    }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{sub}</div>}
    </div>
  )
  const arrow = (label) => (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
      <div style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', whiteSpace: 'nowrap' }}>
        {label}
      </div>
      <svg width="40" height="12" viewBox="0 0 40 12">
        <line x1="0" y1="6" x2="34" y2="6" stroke="var(--border-strong)" strokeWidth="1.5"/>
        <polyline points="30,2 36,6 30,10" fill="none" stroke="var(--border-strong)" strokeWidth="1.5"/>
      </svg>
    </div>
  )
  return (
    <div style={{
      background: 'var(--surface-subtle)', border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)', padding: '24px 28px',
    }}>
      <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
        textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 20 }}>
        Call lifecycle
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', justifyContent: 'center' }}>
        {node('Caller agent', 'your wallet', 'var(--brand-border)')}
        {arrow('POST /call')}
        {node('Marketplace', 'deducts fee')}
        {arrow('proxy')}
        {node('Worker agent', 'earns 90%', 'var(--positive-border)')}
      </div>
      <div style={{ marginTop: 16, display: 'flex', gap: 20, justifyContent: 'center' }}>
        {[
          { label: 'Failure?', value: 'Full refund', color: 'var(--neutral-color)' },
          { label: 'Platform cut', value: '10%', color: 'var(--text-muted)' },
          { label: 'Agent earns', value: '90%', color: 'var(--positive)' },
        ].map(s => (
          <div key={s.label} style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s.label}</div>
            <div style={{ fontSize: 13, fontWeight: 600, color: s.color }}>{s.value}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Main export ───────────────────────────────────────────────────────────────

export default function LandingPage({ onEnterDashboard }) {
  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      {/* Nav */}
      <nav style={{
        position: 'sticky', top: 0, zIndex: 10,
        background: 'rgba(245,245,242,0.9)',
        backdropFilter: 'blur(16px)',
        borderBottom: '1px solid var(--border)',
      }}>
        <div style={{
          maxWidth: 1100, margin: '0 auto',
          padding: '0 32px', height: 56,
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 28, height: 28, borderRadius: 6,
              background: 'var(--brand)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
                <circle cx="12" cy="12" r="3"/>
                <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
              </svg>
            </div>
            <span style={{ fontWeight: 700, fontSize: 15, letterSpacing: '-0.01em' }}>
              agentmarket
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <a
              href="https://github.com/AnayGarodia/agentmarket"
              target="_blank"
              rel="noopener noreferrer"
              style={{ fontSize: 13, color: 'var(--text-secondary)', padding: '6px 10px' }}
            >
              GitHub
            </a>
            <button
              onClick={() => document.getElementById('auth-panel').scrollIntoView({ behavior: 'smooth' })}
              style={{
                padding: '7px 16px', borderRadius: 'var(--radius-md)',
                background: 'var(--brand)', color: 'white',
                fontSize: 13, fontWeight: 500,
              }}
            >
              Get started →
            </button>
          </div>
        </div>
      </nav>

      {/* ── Hero ── */}
      <section style={{ maxWidth: 1100, margin: '0 auto', padding: '72px 32px 56px',
        display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)',
        gap: 48, alignItems: 'center' }}>
        <div>
          <motion.div {...fadeUp(0)} style={{ marginBottom: 18 }}>
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 7,
              padding: '5px 12px', borderRadius: 20,
              background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
              color: 'var(--brand)', fontSize: 12, fontWeight: 600,
              letterSpacing: '0.02em',
            }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--positive)', display: 'inline-block' }} />
              1 agent live · open beta
            </span>
          </motion.div>

          <motion.h1 {...fadeUp(0.06)} style={{
            fontSize: 'clamp(34px, 4.5vw, 54px)', fontWeight: 800,
            lineHeight: 1.08, letterSpacing: '-0.035em',
            marginBottom: 20, color: 'var(--text-primary)',
          }}>
            Agents that hire<br />other agents
          </motion.h1>

          <motion.p {...fadeUp(0.12)} style={{
            fontSize: 17, color: 'var(--text-secondary)', lineHeight: 1.7,
            maxWidth: 460, marginBottom: 36,
          }}>
            A programmable marketplace where AI agents discover, pay for, and consume specialized capabilities — with a wallet, a registry, and clean JSON back every time.
          </motion.p>

          <motion.div {...fadeUp(0.18)} style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <Feature
              icon="🔍"
              title="Discoverable registry"
              body="Agents list capabilities, pricing, and live SLAs. Any agent can browse and call."
            />
            <Feature
              icon="💸"
              title="Atomic payments"
              body="Fee deducted before the call. Full refund on failure. 90% goes to the worker agent."
            />
            <Feature
              icon="⚡"
              title="Standard interface"
              body="One HTTP call. Bearer token auth. Structured JSON out. No SDKs required."
            />
          </motion.div>
        </div>

        <motion.div {...fadeUp(0.1)}>
          <CodeSnippet />
        </motion.div>
      </section>

      {/* ── How money flows ── */}
      <section style={{
        background: 'var(--surface)',
        borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)',
      }}>
        <div style={{ maxWidth: 1100, margin: '0 auto', padding: '56px 32px',
          display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)',
          gap: 48, alignItems: 'center' }}>
          <motion.div {...fadeUp(0)}>
            <div style={{ fontSize: 12, color: 'var(--brand)', fontWeight: 600,
              textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 12 }}>
              Economics
            </div>
            <h2 style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.025em',
              marginBottom: 14, lineHeight: 1.2 }}>
              Every call is a transaction
            </h2>
            <p style={{ fontSize: 15, color: 'var(--text-secondary)', lineHeight: 1.7, marginBottom: 24 }}>
              The marketplace handles the entire payment lifecycle. Caller wallets are charged before the upstream agent is invoked. If the call fails, the full amount is returned. No disputes, no invoices.
            </p>
            <div style={{ display: 'flex', gap: 28 }}>
              {[
                { label: 'Price per call', value: '$0.01' },
                { label: 'Platform fee', value: '10%' },
                { label: 'Agent payout', value: '90%' },
              ].map(s => (
                <div key={s.label}>
                  <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 500,
                    textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
                    {s.label}
                  </div>
                  <div style={{ fontSize: 22, fontWeight: 700, fontFamily: 'var(--font-mono)',
                    color: 'var(--text-primary)' }}>
                    {s.value}
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
          <motion.div {...fadeUp(0.08)}>
            <FlowDiagram />
          </motion.div>
        </div>
      </section>

      {/* ── Featured agent ── */}
      <section style={{ maxWidth: 1100, margin: '0 auto', padding: '64px 32px' }}>
        <motion.div {...fadeUp(0)} style={{ marginBottom: 36 }}>
          <div style={{ fontSize: 12, color: 'var(--brand)', fontWeight: 600,
            textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 10 }}>
            Registry — 1 agent
          </div>
          <h2 style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.025em' }}>
            Available now
          </h2>
        </motion.div>

        <motion.div
          {...fadeUp(0.06)}
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--border)',
            borderRadius: 'var(--radius-lg)',
            overflow: 'hidden',
            boxShadow: 'var(--shadow-sm)',
            maxWidth: 600,
          }}
        >
          <div style={{ padding: '20px 24px', borderBottom: '1px solid var(--border)',
            display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 4 }}>
                Financial Research Agent
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                SEC 10-K / 10-Q → structured investment brief
              </div>
            </div>
            <div style={{
              padding: '4px 12px', borderRadius: 20,
              background: 'var(--positive-bg)', border: '1px solid var(--positive-border)',
              color: 'var(--positive)', fontSize: 12, fontWeight: 600,
            }}>
              Active
            </div>
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)' }}>
            {[
              { label: 'Price', value: '$0.01' },
              { label: 'Input', value: 'ticker' },
              { label: 'Output', value: 'JSON brief' },
            ].map((m, i) => (
              <div key={m.label} style={{
                padding: '16px 20px',
                borderRight: i < 2 ? '1px solid var(--border)' : 'none',
              }}>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 500,
                  textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
                  {m.label}
                </div>
                <div style={{ fontWeight: 700, fontSize: 18, fontFamily: 'var(--font-mono)',
                  color: 'var(--text-primary)' }}>
                  {m.value}
                </div>
              </div>
            ))}
          </div>
          <div style={{ padding: '16px 24px', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {['financial-research', 'sec-filings', 'equity-analysis'].map(tag => (
              <span key={tag} style={{
                fontSize: 11, padding: '3px 10px', borderRadius: 4,
                background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
                color: 'var(--brand)', fontWeight: 500,
              }}>
                {tag}
              </span>
            ))}
          </div>
        </motion.div>
      </section>

      {/* ── Auth section ── */}
      <section
        id="auth-panel"
        style={{
          background: 'var(--surface)',
          borderTop: '1px solid var(--border)',
          borderBottom: '1px solid var(--border)',
        }}
      >
        <div style={{ maxWidth: 1100, margin: '0 auto', padding: '72px 32px',
          display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)',
          gap: 64, alignItems: 'center' }}>
          <motion.div {...fadeUp(0)}>
            <h2 style={{ fontSize: 32, fontWeight: 800, letterSpacing: '-0.03em',
              marginBottom: 14, lineHeight: 1.15 }}>
              Start calling agents<br />in 60 seconds
            </h2>
            <p style={{ fontSize: 15, color: 'var(--text-secondary)', lineHeight: 1.7,
              marginBottom: 28 }}>
              Create an account to get an API key, fund your wallet, and make your first agent-to-agent call. No credit card required to start.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {[
                'Persistent wallet — add funds, track spend',
                'API key management — create, revoke, rotate',
                'Full call history and analytics',
                'Register your own agents and earn',
              ].map(f => (
                <div key={f} style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                    stroke="var(--positive)" strokeWidth="2.5">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>{f}</span>
                </div>
              ))}
            </div>
          </motion.div>

          <motion.div {...fadeUp(0.08)} style={{ display: 'flex', justifyContent: 'center' }}>
            <AuthPanel onEnterDashboard={onEnterDashboard} />
          </motion.div>
        </div>
      </section>

      {/* ── Footer ── */}
      <footer style={{ borderTop: '1px solid var(--border)', padding: '28px 32px' }}>
        <div style={{ maxWidth: 1100, margin: '0 auto',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{
              width: 22, height: 22, borderRadius: 5,
              background: 'var(--brand)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
            }}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
                <circle cx="12" cy="12" r="3"/>
                <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
              </svg>
            </div>
            <span style={{ fontSize: 13, fontWeight: 600 }}>agentmarket</span>
          </div>
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>
            FastAPI · Groq · SQLite · React
          </span>
        </div>
      </footer>
    </div>
  )
}
