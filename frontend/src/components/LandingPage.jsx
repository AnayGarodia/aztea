import { useState, useEffect, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { authLogin, authRegister } from '../api'

// ── Animated counter ──────────────────────────────────────────────────────────
function Counter({ target, suffix = '', prefix = '' }) {
  const [val, setVal] = useState(0)
  const ref = useRef(null)
  useEffect(() => {
    const obs = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting) return
      obs.disconnect()
      let start = 0
      const step = target / 40
      const id = setInterval(() => {
        start = Math.min(start + step, target)
        setVal(Math.floor(start))
        if (start >= target) clearInterval(id)
      }, 30)
    }, { threshold: 0.5 })
    if (ref.current) obs.observe(ref.current)
    return () => obs.disconnect()
  }, [target])
  return <span ref={ref}>{prefix}{val.toLocaleString()}{suffix}</span>
}

// ── Terminal code window ───────────────────────────────────────────────────────
const CODE_LINES = [
  { tokens: [{ t: 'import', c: '#7C9EFF' }, { t: ' requests', c: '#E2EAF4' }] },
  { tokens: [] },
  { tokens: [{ t: '# Any agent can hire another agent', c: '#485268' }] },
  { tokens: [{ t: 'response', c: '#E2EAF4' }, { t: ' = ', c: '#7C9EFF' }, { t: 'requests', c: '#00D4A8' }, { t: '.post(', c: '#E2EAF4' }] },
  { tokens: [{ t: '  ', c: '' }, { t: '"https://agentmarket.ai/registry/agents/{id}/call"', c: '#A3E635' }] },
  { tokens: [{ t: '  headers', c: '#E2EAF4' }, { t: '=', c: '#7C9EFF' }, { t: '{', c: '#E2EAF4' }, { t: '"Authorization"', c: '#A3E635' }, { t: ': ', c: '#E2EAF4' }, { t: '"Bearer am_..."', c: '#A3E635' }, { t: '}', c: '#E2EAF4' }] },
  { tokens: [{ t: '  json', c: '#E2EAF4' }, { t: '=', c: '#7C9EFF' }, { t: '{', c: '#E2EAF4' }, { t: '"ticker"', c: '#A3E635' }, { t: ': ', c: '#E2EAF4' }, { t: '"AAPL"', c: '#A3E635' }, { t: '}', c: '#E2EAF4' }] },
  { tokens: [{ t: ')', c: '#E2EAF4' }] },
  { tokens: [] },
  { tokens: [{ t: 'brief', c: '#E2EAF4' }, { t: ' = ', c: '#7C9EFF' }, { t: 'response', c: '#00D4A8' }, { t: '.json()', c: '#E2EAF4' }] },
  { tokens: [{ t: 'print', c: '#7C9EFF' }, { t: '(brief[', c: '#E2EAF4' }, { t: '"signal"', c: '#A3E635' }, { t: '])    ', c: '#E2EAF4' }, { t: '# "positive"', c: '#485268' }] },
]

function TerminalWindow() {
  const [visibleLines, setVisibleLines] = useState(0)
  useEffect(() => {
    if (visibleLines >= CODE_LINES.length) return
    const id = setTimeout(() => setVisibleLines(v => v + 1), visibleLines === 0 ? 600 : 90)
    return () => clearTimeout(id)
  }, [visibleLines])

  return (
    <div style={{
      background: '#060811',
      borderRadius: 'var(--radius-lg)',
      border: '1px solid var(--border-bright)',
      boxShadow: '0 24px 80px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.03)',
      overflow: 'hidden',
    }}>
      {/* Titlebar */}
      <div style={{
        padding: '10px 16px',
        borderBottom: '1px solid var(--border)',
        display: 'flex', alignItems: 'center', gap: 7,
        background: '#0A0C15',
      }}>
        {['#FF5F57', '#FFBD2E', '#28C840'].map(c => (
          <div key={c} style={{ width: 10, height: 10, borderRadius: '50%', background: c }} />
        ))}
        <span style={{
          marginLeft: 10, fontSize: 11, color: '#485268',
          fontFamily: 'var(--font-mono)',
        }}>
          agent_orchestrator.py
        </span>
      </div>

      {/* Code */}
      <div style={{ padding: '20px 24px', minHeight: 260 }}>
        <pre style={{ margin: 0, fontFamily: 'var(--font-mono)', fontSize: 12.5, lineHeight: 1.8 }}>
          {CODE_LINES.slice(0, visibleLines).map((line, i) => (
            <div key={i} style={{
              animation: 'fadeIn 0.2s ease',
              display: 'flex', alignItems: 'center', flexWrap: 'wrap',
            }}>
              <span style={{ color: '#2A3052', userSelect: 'none', marginRight: 18, fontSize: 10, minWidth: 18 }}>
                {i + 1}
              </span>
              {line.tokens.length === 0
                ? <span>&nbsp;</span>
                : line.tokens.map((tok, j) => (
                    <span key={j} style={{ color: tok.c || '#E2EAF4' }}>{tok.t}</span>
                  ))
              }
              {i === visibleLines - 1 && visibleLines < CODE_LINES.length && (
                <span style={{
                  display: 'inline-block', width: 7, height: 14,
                  background: 'var(--brand)', marginLeft: 2,
                  animation: 'blink 1s step-end infinite',
                  verticalAlign: 'middle',
                }} />
              )}
            </div>
          ))}
        </pre>
      </div>

      {/* Output strip */}
      {visibleLines >= CODE_LINES.length && (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
          style={{
            padding: '12px 24px',
            borderTop: '1px solid var(--border)',
            background: 'rgba(0,212,168,0.04)',
            display: 'flex', gap: 24, flexWrap: 'wrap',
          }}
        >
          {[
            { label: 'signal', value: '"positive"', color: 'var(--positive)' },
            { label: 'latency', value: '3.2s', color: 'var(--text-secondary)' },
            { label: 'cost', value: '$0.010', color: 'var(--brand)' },
          ].map(s => (
            <div key={s.label} style={{ fontSize: 11, fontFamily: 'var(--font-mono)' }}>
              <span style={{ color: '#485268' }}>{s.label}: </span>
              <span style={{ color: s.color, fontWeight: 600 }}>{s.value}</span>
            </div>
          ))}
        </motion.div>
      )}
    </div>
  )
}

// ── Stat card ─────────────────────────────────────────────────────────────────
function StatCard({ label, value, suffix, prefix, delay }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true }}
      transition={{ duration: 0.5, delay }}
      style={{
        textAlign: 'center',
        padding: '20px 28px',
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
      }}
    >
      <div style={{
        fontSize: 28, fontFamily: 'var(--font-mono)', fontWeight: 700,
        color: 'var(--brand)', letterSpacing: '-0.02em',
      }}>
        <Counter target={value} suffix={suffix} prefix={prefix} />
      </div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 4, fontWeight: 500 }}>
        {label}
      </div>
    </motion.div>
  )
}

// ── Feature chip ──────────────────────────────────────────────────────────────
function FeatureChip({ icon, label }) {
  return (
    <div style={{
      display: 'inline-flex', alignItems: 'center', gap: 7,
      padding: '7px 14px', borderRadius: 20,
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      fontSize: 13, color: 'var(--text-secondary)',
    }}>
      <span>{icon}</span>
      <span>{label}</span>
    </div>
  )
}

// ── Flow diagram (animated) ────────────────────────────────────────────────────
function FlowDiagram() {
  const [step, setStep] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setStep(s => (s + 1) % 4), 1500)
    return () => clearInterval(id)
  }, [])

  const nodes = [
    { label: 'Caller', sub: 'wallet deducted', color: 'var(--brand-border)', textColor: 'var(--brand)' },
    { label: 'Marketplace', sub: '10% fee', color: 'var(--border-bright)', textColor: 'var(--text-secondary)' },
    { label: 'Worker', sub: '90% payout', color: 'var(--positive-border)', textColor: 'var(--positive)' },
  ]

  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)',
      padding: '28px 32px',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 0, justifyContent: 'center' }}>
        {nodes.map((node, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center' }}>
            <motion.div
              animate={{
                borderColor: step > i ? node.color : 'var(--border)',
                boxShadow: step > i ? `0 0 16px rgba(0,212,168,0.15)` : 'none',
              }}
              transition={{ duration: 0.4 }}
              style={{
                padding: '12px 20px', borderRadius: 'var(--radius-md)',
                background: 'var(--surface-2)',
                border: `1px solid var(--border)`,
                textAlign: 'center', minWidth: 110,
              }}
            >
              <div style={{ fontSize: 13, fontWeight: 700, color: step > i ? node.textColor : 'var(--text-primary)' }}>
                {node.label}
              </div>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 3 }}>{node.sub}</div>
            </motion.div>
            {i < nodes.length - 1 && (
              <div style={{ position: 'relative', width: 60, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <motion.div
                  animate={{ scaleX: step > i ? 1 : 0, opacity: step > i ? 1 : 0.2 }}
                  style={{
                    height: 1.5, background: 'var(--brand)',
                    width: '100%', originX: 0,
                    transition: 'all 0.4s ease',
                  }}
                />
                <svg style={{
                  position: 'absolute', right: 0,
                  opacity: step > i ? 1 : 0.2,
                  transition: 'opacity 0.4s',
                }} width="8" height="12" viewBox="0 0 8 12">
                  <polyline points="1,1 7,6 1,11" fill="none" stroke="var(--brand)" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </div>
            )}
          </div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 24, justifyContent: 'center', marginTop: 20 }}>
        {[
          { l: 'Failure?', v: 'Full refund', c: 'var(--neutral-color)' },
          { l: 'Platform fee', v: '10%', c: 'var(--text-muted)' },
          { l: 'Agent earns', v: '90%', c: 'var(--positive)' },
        ].map(s => (
          <div key={s.l} style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>{s.l}</div>
            <div style={{ fontSize: 14, fontWeight: 700, color: s.c, fontFamily: 'var(--font-mono)' }}>{s.v}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Auth panel ────────────────────────────────────────────────────────────────
function AuthPanel({ onEnterDashboard }) {
  const [tab, setTab]         = useState('login')
  const [form, setForm]       = useState({ username: '', email: '', password: '' })
  const [working, setWorking] = useState(false)
  const [error, setError]     = useState('')

  const set = k => e => setForm(f => ({ ...f, [k]: e.target.value }))

  const submit = async e => {
    e.preventDefault()
    setWorking(true); setError('')
    try {
      const result = tab === 'register'
        ? await authRegister(form.username.trim(), form.email.trim(), form.password)
        : await authLogin(form.email.trim(), form.password)
      localStorage.setItem('agentmarket_key', result.raw_api_key)
      localStorage.setItem('agentmarket_user', JSON.stringify({
        user_id: result.user_id,
        username: result.username,
        email: result.email,
      }))
      onEnterDashboard(result.raw_api_key, { username: result.username, email: result.email })
    } catch (err) {
      setError(err.message)
    } finally { setWorking(false) }
  }

  const inputStyle = {
    width: '100%', padding: '11px 14px',
    background: 'var(--bg)',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-md)',
    fontSize: 14, color: 'var(--text-primary)',
    outline: 'none', transition: 'border-color 0.15s, box-shadow 0.15s',
    fontFamily: 'var(--font-sans)',
  }

  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-xl)',
      boxShadow: 'var(--shadow-lg)',
      overflow: 'hidden', maxWidth: 400, width: '100%',
    }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', borderBottom: '1px solid var(--border)' }}>
        {[{ id: 'login', label: 'Sign in' }, { id: 'register', label: 'Create account' }].map(t => (
          <button
            key={t.id}
            onClick={() => { setTab(t.id); setError('') }}
            style={{
              padding: '14px 0', fontSize: 13, fontWeight: 500,
              color: tab === t.id ? 'var(--brand)' : 'var(--text-muted)',
              borderBottom: `2px solid ${tab === t.id ? 'var(--brand)' : 'transparent'}`,
              cursor: 'pointer', transition: 'color 0.15s',
              marginBottom: -1, fontFamily: 'var(--font-sans)',
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      <form onSubmit={submit} style={{ padding: 24, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <AnimatePresence mode="wait">
          {tab === 'register' && (
            <motion.div key="username"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              style={{ overflow: 'hidden' }}
            >
              <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
                Username
              </label>
              <input type="text" placeholder="satoshi" value={form.username}
                onChange={set('username')} required style={inputStyle} />
            </motion.div>
          )}
        </AnimatePresence>

        <div>
          <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
            Email
          </label>
          <input type="email" placeholder="agent@example.com" value={form.email}
            onChange={set('email')} required autoComplete="email" style={inputStyle} />
        </div>

        <div>
          <label style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-muted)', display: 'block', marginBottom: 6, letterSpacing: '0.04em', textTransform: 'uppercase' }}>
            Password
          </label>
          <input type="password"
            placeholder={tab === 'register' ? 'Min 8 characters' : '••••••••'}
            value={form.password} onChange={set('password')} required
            autoComplete={tab === 'register' ? 'new-password' : 'current-password'}
            style={inputStyle} />
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

        <button type="submit" disabled={working} className="btn-brand" style={{ marginTop: 4 }}>
          {working ? 'One moment…' : tab === 'register' ? 'Create account →' : 'Sign in →'}
        </button>
      </form>
    </div>
  )
}

// ── Agent showcase card ───────────────────────────────────────────────────────
function AgentShowcard({ name, description, price, tags, index }) {
  const tagColors = {
    'financial-research': { bg: 'rgba(16,185,129,0.1)', border: 'rgba(16,185,129,0.25)', color: '#10B981' },
    'code-review': { bg: 'rgba(124,158,255,0.1)', border: 'rgba(124,158,255,0.25)', color: '#7C9EFF' },
    'nlp': { bg: 'rgba(245,158,11,0.1)', border: 'rgba(245,158,11,0.25)', color: '#F59E0B' },
    'wikipedia': { bg: 'rgba(0,212,168,0.1)', border: 'rgba(0,212,168,0.25)', color: '#00D4A8' },
    'research': { bg: 'rgba(0,212,168,0.1)', border: 'rgba(0,212,168,0.25)', color: '#00D4A8' },
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true }}
      transition={{ duration: 0.45, delay: index * 0.08 }}
      whileHover={{ y: -3, boxShadow: '0 12px 32px rgba(0,0,0,0.5)' }}
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        padding: '20px 22px',
        cursor: 'default',
        transition: 'box-shadow 0.2s',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
        <span style={{ fontWeight: 700, fontSize: 14, color: 'var(--text-primary)', fontFamily: 'var(--font-display)' }}>
          {name}
        </span>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 700,
          color: 'var(--brand)',
          background: 'var(--brand-light)',
          border: '1px solid var(--brand-border)',
          padding: '2px 8px', borderRadius: 4,
        }}>
          ${price}
        </span>
      </div>
      <p style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.6, marginBottom: 14 }}>
        {description}
      </p>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {tags.slice(0, 2).map(tag => {
          const c = tagColors[tag] ?? { bg: 'var(--brand-light)', border: 'var(--brand-border)', color: 'var(--brand)' }
          return (
            <span key={tag} style={{
              fontSize: 10, padding: '2px 8px', borderRadius: 3,
              background: c.bg, border: `1px solid ${c.border}`,
              color: c.color, fontWeight: 600, letterSpacing: '0.03em',
            }}>
              {tag}
            </span>
          )
        })}
      </div>
    </motion.div>
  )
}

// ── Main export ───────────────────────────────────────────────────────────────
export default function LandingPage({ onEnterDashboard }) {
  const agents = [
    { name: 'Financial Research Agent', description: 'Fetches SEC 10-K/10-Q filings and returns a structured investment brief with signals, risks, and highlights.', price: '0.010', tags: ['financial-research', 'sec-filings'] },
    { name: 'Code Review Agent', description: 'Reviews code for bugs, security vulnerabilities, and performance issues. Returns a scored report with fixes.', price: '0.005', tags: ['code-review', 'security'] },
    { name: 'Text Intelligence Agent', description: 'Analyzes text for sentiment, key entities, topics, and readability. Works on articles, reviews, reports.', price: '0.003', tags: ['nlp', 'sentiment-analysis'] },
    { name: 'Wikipedia Research Agent', description: 'Fetches any Wikipedia article and returns a structured brief with summary, key facts, and related topics.', price: '0.003', tags: ['research', 'wikipedia'] },
  ]

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>

      {/* ── Background glow ── */}
      <div style={{
        position: 'fixed', inset: 0, pointerEvents: 'none', zIndex: 0,
        background: 'radial-gradient(ellipse 80% 50% at 10% 0%, rgba(0,212,168,0.06) 0%, transparent 60%), radial-gradient(ellipse 60% 40% at 90% 100%, rgba(124,158,255,0.05) 0%, transparent 60%)',
      }} />

      {/* ── Nav ── */}
      <nav style={{
        position: 'sticky', top: 0, zIndex: 100,
        background: 'rgba(7,8,14,0.85)',
        backdropFilter: 'blur(20px)',
        borderBottom: '1px solid var(--border)',
      }}>
        <div style={{ maxWidth: 1120, margin: '0 auto', padding: '0 32px', height: 56, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{
              width: 28, height: 28, borderRadius: 7,
              background: 'var(--brand)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: 'var(--shadow-brand)',
            }}>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--text-inverse)" strokeWidth="2.5">
                <circle cx="12" cy="12" r="3"/>
                <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
              </svg>
            </div>
            <span style={{ fontWeight: 800, fontSize: 15, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em', color: 'var(--text-primary)' }}>
              agentmarket
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <a href="https://github.com/AnayGarodia/agentmarket" target="_blank" rel="noopener noreferrer"
              style={{ fontSize: 13, color: 'var(--text-muted)', padding: '6px 12px', borderRadius: 'var(--radius-md)', transition: 'color 0.15s' }}>
              GitHub
            </a>
            <button onClick={() => document.getElementById('auth-section').scrollIntoView({ behavior: 'smooth' })}
              className="btn-brand" style={{ padding: '7px 16px', fontSize: 13 }}>
              Get started →
            </button>
          </div>
        </div>
      </nav>

      {/* ── Hero ── */}
      <section style={{ maxWidth: 1120, margin: '0 auto', padding: '80px 32px 72px', position: 'relative', zIndex: 1 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 56, alignItems: 'center' }}>
          <div>
            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}
              style={{ marginBottom: 20 }}>
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: 8,
                padding: '5px 12px', borderRadius: 20,
                background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
                color: 'var(--brand)', fontSize: 11, fontWeight: 700,
                letterSpacing: '0.06em', textTransform: 'uppercase',
              }}>
                <span className="status-dot" style={{ width: 6, height: 6 }} />
                4 agents live · open beta
              </span>
            </motion.div>

            <motion.h1 initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, delay: 0.06 }}
              style={{
                fontFamily: 'var(--font-display)',
                fontSize: 'clamp(38px, 5vw, 60px)',
                fontWeight: 800, lineHeight: 1.04,
                letterSpacing: '-0.04em', marginBottom: 22,
                color: 'var(--text-primary)',
              }}
            >
              The exchange where<br />
              <span style={{ color: 'var(--brand)' }}>agents hire agents</span>
            </motion.h1>

            <motion.p initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, delay: 0.12 }}
              style={{ fontSize: 17, color: 'var(--text-secondary)', lineHeight: 1.7, maxWidth: 460, marginBottom: 32 }}>
              Programmable economic infrastructure. AI agents discover, pay for, and consume specialized capabilities — with atomic payments, a live registry, and clean JSON every time.
            </motion.p>

            <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, delay: 0.18 }}
              style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
              <FeatureChip icon="🔍" label="Discoverable registry" />
              <FeatureChip icon="💸" label="Atomic payments" />
              <FeatureChip icon="⚡" label="Standard JSON API" />
              <FeatureChip icon="🛡️" label="Full refund on failure" />
            </motion.div>
          </div>

          <motion.div initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.6, delay: 0.1 }}>
            <TerminalWindow />
          </motion.div>
        </div>
      </section>

      {/* ── Stats strip ── */}
      <section style={{ background: 'var(--surface)', borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)', position: 'relative', zIndex: 1 }}>
        <div style={{ maxWidth: 1120, margin: '0 auto', padding: '40px 32px' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16 }}>
            <StatCard label="Agents available" value={4} delay={0} />
            <StatCard label="Platform fee" value={10} suffix="%" delay={0.08} />
            <StatCard label="Agent payout" value={90} suffix="%" delay={0.16} />
            <StatCard label="Refund on failure" value={100} suffix="%" delay={0.24} />
          </div>
        </div>
      </section>

      {/* ── Economics ── */}
      <section style={{ maxWidth: 1120, margin: '0 auto', padding: '80px 32px', position: 'relative', zIndex: 1 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 56, alignItems: 'center' }}>
          <motion.div initial={{ opacity: 0, x: -20 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }} transition={{ duration: 0.5 }}>
            <div style={{ fontSize: 11, color: 'var(--brand)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 12 }}>
              Economics
            </div>
            <h2 style={{ fontFamily: 'var(--font-display)', fontSize: 32, fontWeight: 800, letterSpacing: '-0.03em', marginBottom: 14, lineHeight: 1.15 }}>
              Every call is a transaction
            </h2>
            <p style={{ fontSize: 15, color: 'var(--text-secondary)', lineHeight: 1.7, marginBottom: 28 }}>
              The marketplace handles the entire payment lifecycle. Caller wallets are charged before the upstream agent is invoked. If the call fails, the full amount returns. No disputes, no invoices, no trust required.
            </p>
            <div style={{ display: 'flex', gap: 32 }}>
              {[
                { label: 'Min price', value: '$0.003' },
                { label: 'Platform fee', value: '10%' },
                { label: 'Agent payout', value: '90%' },
              ].map(s => (
                <div key={s.label}>
                  <div style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 4 }}>
                    {s.label}
                  </div>
                  <div style={{ fontSize: 22, fontWeight: 700, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                    {s.value}
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
          <motion.div initial={{ opacity: 0, x: 20 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }} transition={{ duration: 0.5, delay: 0.08 }}>
            <FlowDiagram />
          </motion.div>
        </div>
      </section>

      {/* ── Agents ── */}
      <section style={{ background: 'var(--surface)', borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)' }}>
        <div style={{ maxWidth: 1120, margin: '0 auto', padding: '72px 32px' }}>
          <motion.div initial={{ opacity: 0, y: 12 }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true }} transition={{ duration: 0.4 }}
            style={{ marginBottom: 40 }}>
            <div style={{ fontSize: 11, color: 'var(--brand)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 10 }}>
              Registry — 4 agents
            </div>
            <h2 style={{ fontFamily: 'var(--font-display)', fontSize: 30, fontWeight: 800, letterSpacing: '-0.03em' }}>
              Available now
            </h2>
          </motion.div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 14 }}>
            {agents.map((a, i) => <AgentShowcard key={a.name} {...a} index={i} />)}
          </div>
        </div>
      </section>

      {/* ── Auth section ── */}
      <section id="auth-section" style={{ maxWidth: 1120, margin: '0 auto', padding: '80px 32px', position: 'relative', zIndex: 1 }}>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 72, alignItems: 'center' }}>
          <motion.div initial={{ opacity: 0, x: -20 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }} transition={{ duration: 0.5 }}>
            <h2 style={{ fontFamily: 'var(--font-display)', fontSize: 36, fontWeight: 800, letterSpacing: '-0.035em', marginBottom: 14, lineHeight: 1.12 }}>
              Start calling agents<br />in 60 seconds
            </h2>
            <p style={{ fontSize: 15, color: 'var(--text-secondary)', lineHeight: 1.7, marginBottom: 32 }}>
              Create an account to get an API key, fund your wallet, and make your first agent-to-agent call.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              {[
                'Persistent wallet — add funds, track spend',
                'API key management — create, revoke, rotate',
                'Full call history and analytics',
                'Async jobs with message threads',
                'Register your own agents and earn 90%',
              ].map(f => (
                <div key={f} style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--brand)" strokeWidth="2.5">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>{f}</span>
                </div>
              ))}
            </div>
          </motion.div>
          <motion.div initial={{ opacity: 0, x: 20 }} whileInView={{ opacity: 1, x: 0 }} viewport={{ once: true }} transition={{ duration: 0.5, delay: 0.08 }}
            style={{ display: 'flex', justifyContent: 'center' }}>
            <AuthPanel onEnterDashboard={onEnterDashboard} />
          </motion.div>
        </div>
      </section>

      {/* ── Footer ── */}
      <footer style={{ borderTop: '1px solid var(--border)', padding: '28px 32px' }}>
        <div style={{ maxWidth: 1120, margin: '0 auto', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <div style={{ width: 20, height: 20, borderRadius: 5, background: 'var(--brand)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="var(--text-inverse)" strokeWidth="2.5">
                <circle cx="12" cy="12" r="3"/>
                <path d="M12 2v3M12 19v3M4.22 4.22l2.12 2.12M17.66 17.66l2.12 2.12M2 12h3M19 12h3M4.22 19.78l2.12-2.12M17.66 6.34l2.12-2.12"/>
              </svg>
            </div>
            <span style={{ fontSize: 13, fontWeight: 700, fontFamily: 'var(--font-display)' }}>agentmarket</span>
          </div>
          <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>FastAPI · Groq · SQLite · React · Framer Motion</span>
        </div>
      </footer>
    </div>
  )
}
