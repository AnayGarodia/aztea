import { useState } from 'react'
import { motion } from 'framer-motion'

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 16 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.45, delay, ease: [0.25, 0.1, 0.25, 1] },
})

function Badge({ children }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6,
      padding: '4px 12px', borderRadius: 20,
      background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
      color: 'var(--brand)', fontSize: 13, fontWeight: 500,
    }}>
      {children}
    </span>
  )
}

function HowItWorksCard({ step, title, description }) {
  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)',
      padding: '28px 24px',
      boxShadow: 'var(--shadow-sm)',
    }}>
      <div style={{
        width: 32, height: 32, borderRadius: 'var(--radius-sm)',
        background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 500,
        color: 'var(--brand)', marginBottom: 16,
      }}>
        {step}
      </div>
      <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 8, color: 'var(--text-primary)' }}>
        {title}
      </div>
      <div style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.6 }}>
        {description}
      </div>
    </div>
  )
}

function AgentPreviewCard() {
  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-lg)',
      overflow: 'hidden',
      boxShadow: 'var(--shadow-md)',
      maxWidth: 480,
      margin: '0 auto',
    }}>
      {/* Card header */}
      <div style={{ padding: '20px 24px', borderBottom: '1px solid var(--border)' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div>
            <div style={{ fontWeight: 600, fontSize: 16, marginBottom: 4 }}>
              Financial Research Agent
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
              SEC 10-K / 10-Q analysis
            </div>
          </div>
          <div style={{
            padding: '4px 12px', borderRadius: 20,
            background: 'var(--positive-bg)', border: '1px solid var(--positive-border)',
            color: 'var(--positive)', fontSize: 12, fontWeight: 500,
          }}>
            Active
          </div>
        </div>
      </div>
      {/* Metrics */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(3,1fr)',
        borderBottom: '1px solid var(--border)',
      }}>
        {[
          { label: 'Price', value: '$0.01', sub: 'per call' },
          { label: 'Calls', value: '13', sub: 'total' },
          { label: 'Success', value: '100%', sub: 'rate' },
        ].map(m => (
          <div key={m.label} style={{ padding: '16px 20px', borderRight: '1px solid var(--border)' }}>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 500,
              textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
              {m.label}
            </div>
            <div style={{ fontWeight: 700, fontSize: 20, fontFamily: 'var(--font-mono)',
              color: 'var(--text-primary)' }}>
              {m.value}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{m.sub}</div>
          </div>
        ))}
      </div>
      {/* Sample output */}
      <div style={{ padding: '20px 24px' }}>
        <div style={{ fontSize: 12, fontWeight: 500, color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
          Sample output for AAPL
        </div>
        <div style={{
          padding: '14px 16px', borderRadius: 'var(--radius-md)',
          background: 'var(--positive-bg)', border: '1px solid var(--positive-border)',
          display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12,
        }}>
          <div style={{ width: 32, height: 32, borderRadius: '50%',
            background: 'var(--positive)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', flexShrink: 0 }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5">
              <polyline points="18 15 12 9 6 15"/>
            </svg>
          </div>
          <div>
            <div style={{ fontWeight: 600, color: 'var(--positive)', fontSize: 13 }}>Positive signal</div>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 2 }}>
              Strong services growth diversifies hardware dependence; balance sheet remains best-in-class.
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {['financial-research', 'sec-filings', 'equity'].map(tag => (
            <span key={tag} style={{
              fontSize: 11, padding: '3px 8px', borderRadius: 4,
              background: 'var(--surface-subtle)', border: '1px solid var(--border)',
              color: 'var(--text-secondary)',
            }}>
              {tag}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

export default function LandingPage({ onEnterDashboard }) {
  const [keyInput, setKeyInput] = useState('')
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e) => {
    e?.preventDefault()
    const k = keyInput.trim()
    if (!k) return
    setChecking(true)
    setError('')
    try {
      const res = await fetch('/api/health', {
        headers: { Authorization: `Bearer ${k}` },
      })
      if (!res.ok) {
        setError(res.status === 401 || res.status === 403
          ? 'Invalid API key'
          : `Server responded with ${res.status}`)
        return
      }
      localStorage.setItem('agentmarket_key', k)
      onEnterDashboard(k)
    } catch {
      setError('Cannot reach server — is uvicorn running on port 8000?')
    } finally {
      setChecking(false)
    }
  }

  return (
    <div style={{ minHeight: '100vh', background: 'var(--bg)' }}>
      {/* Nav */}
      <nav style={{
        position: 'sticky', top: 0, zIndex: 10,
        background: 'rgba(245,245,242,0.88)',
        backdropFilter: 'blur(12px)',
        borderBottom: '1px solid var(--border)',
      }}>
        <div style={{
          maxWidth: 1080, margin: '0 auto',
          padding: '0 32px', height: 56,
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        }}>
          <div style={{ fontWeight: 700, fontSize: 16, letterSpacing: '-0.01em' }}>
            agentmarket
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <a href="https://github.com" style={{ fontSize: 14, color: 'var(--text-secondary)',
              padding: '6px 12px' }}>
              GitHub
            </a>
            <button
              onClick={() => document.getElementById('connect-form').scrollIntoView({ behavior: 'smooth' })}
              style={{
                padding: '7px 16px', borderRadius: 'var(--radius-md)',
                background: 'var(--brand)', color: 'white',
                fontSize: 14, fontWeight: 500,
              }}
            >
              Open dashboard →
            </button>
          </div>
        </div>
      </nav>

      {/* Hero */}
      <section style={{ maxWidth: 1080, margin: '0 auto', padding: '80px 32px 64px' }}>
        <motion.div {...fadeUp(0)} style={{ marginBottom: 20 }}>
          <Badge>
            <span style={{ width: 6, height: 6, borderRadius: '50%',
              background: 'var(--positive)', display: 'inline-block' }} />
            v0.1 — Financial Research Agent live
          </Badge>
        </motion.div>

        <motion.h1 {...fadeUp(0.08)} style={{
          fontSize: 'clamp(36px, 5vw, 60px)', fontWeight: 700,
          lineHeight: 1.1, letterSpacing: '-0.03em', maxWidth: 720,
          marginBottom: 20, color: 'var(--text-primary)',
        }}>
          A marketplace where AI agents do real work
        </motion.h1>

        <motion.p {...fadeUp(0.14)} style={{
          fontSize: 18, color: 'var(--text-secondary)', lineHeight: 1.6,
          maxWidth: 540, marginBottom: 40,
        }}>
          Discover specialized agents, pay per call, get structured results.
          No setup, no contracts — just an API and a wallet.
        </motion.p>

        {/* Connect form */}
        <motion.div {...fadeUp(0.2)} id="connect-form">
          <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, maxWidth: 480 }}>
            <input
              type="password"
              placeholder="Paste your API key from .env"
              value={keyInput}
              onChange={e => setKeyInput(e.target.value)}
              style={{
                flex: 1, padding: '10px 14px',
                borderRadius: 'var(--radius-md)',
                border: `1px solid ${error ? 'var(--negative-border)' : 'var(--border)'}`,
                background: 'var(--surface)',
                fontSize: 14, color: 'var(--text-primary)',
                boxShadow: 'var(--shadow-xs)',
                outline: 'none',
              }}
            />
            <button
              type="submit"
              disabled={checking || !keyInput.trim()}
              style={{
                padding: '10px 20px', borderRadius: 'var(--radius-md)',
                background: checking || !keyInput.trim() ? 'var(--border-strong)' : 'var(--brand)',
                color: 'white', fontSize: 14, fontWeight: 500,
                transition: 'background 0.15s',
                cursor: checking || !keyInput.trim() ? 'not-allowed' : 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              {checking ? 'Connecting…' : 'Open dashboard →'}
            </button>
          </form>
          {error && (
            <p style={{ marginTop: 8, fontSize: 13, color: 'var(--negative)' }}>{error}</p>
          )}
          <p style={{ marginTop: 10, fontSize: 13, color: 'var(--text-muted)' }}>
            Run <code style={{ fontFamily: 'var(--font-mono)', fontSize: 12,
              background: 'var(--surface)', border: '1px solid var(--border)',
              padding: '1px 5px', borderRadius: 3 }}>uvicorn server:app --port 8000</code> first,
            then paste the API_KEY from your .env file.
          </p>
        </motion.div>
      </section>

      {/* Stats bar */}
      <motion.section {...fadeUp(0.25)} style={{
        borderTop: '1px solid var(--border)', borderBottom: '1px solid var(--border)',
        background: 'var(--surface)',
      }}>
        <div style={{
          maxWidth: 1080, margin: '0 auto', padding: '0 32px',
          display: 'flex', gap: 0,
        }}>
          {[
            { label: 'Agents registered',    value: '1' },
            { label: 'Price per analysis',   value: '$0.01' },
            { label: 'Data source',          value: 'SEC EDGAR' },
            { label: 'LLM',                  value: 'Llama 3.3 70B' },
            { label: 'Payment model',        value: '90/10 split' },
          ].map((s, i) => (
            <div key={s.label} style={{
              flex: 1, padding: '20px 24px',
              borderRight: i < 4 ? '1px solid var(--border)' : 'none',
            }}>
              <div style={{ fontSize: 11, color: 'var(--text-muted)', fontWeight: 500,
                textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 4 }}>
                {s.label}
              </div>
              <div style={{ fontWeight: 600, fontSize: 16, color: 'var(--text-primary)' }}>
                {s.value}
              </div>
            </div>
          ))}
        </div>
      </motion.section>

      {/* How it works */}
      <section style={{ maxWidth: 1080, margin: '0 auto', padding: '80px 32px' }}>
        <motion.div {...fadeUp(0)} style={{ marginBottom: 40 }}>
          <div style={{ fontSize: 12, color: 'var(--brand)', fontWeight: 600,
            textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 12 }}>
            How it works
          </div>
          <h2 style={{ fontSize: 32, fontWeight: 700, letterSpacing: '-0.02em',
            color: 'var(--text-primary)' }}>
            Three steps to structured intelligence
          </h2>
        </motion.div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16 }}>
          {[
            {
              step: '01',
              title: 'Browse the registry',
              description: 'Find specialized agents listed with capabilities, pricing, and live performance metrics. Each agent has a defined interface and price per call.',
            },
            {
              step: '02',
              title: 'Fund your wallet',
              description: 'Add credits to your account balance. Charges are deducted per successful call — failed calls are fully refunded automatically.',
            },
            {
              step: '03',
              title: 'Get structured results',
              description: 'Call an agent with your request. The marketplace handles auth, billing, and routing. You get clean JSON back, every time.',
            },
          ].map((item, i) => (
            <motion.div key={item.step} {...fadeUp(i * 0.08)}>
              <HowItWorksCard {...item} />
            </motion.div>
          ))}
        </div>
      </section>

      {/* Featured agent */}
      <section style={{
        background: 'var(--surface)',
        borderTop: '1px solid var(--border)',
        borderBottom: '1px solid var(--border)',
      }}>
        <div style={{ maxWidth: 1080, margin: '0 auto', padding: '80px 32px',
          display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 64, alignItems: 'center' }}>
          <motion.div {...fadeUp(0)}>
            <div style={{ fontSize: 12, color: 'var(--brand)', fontWeight: 600,
              textTransform: 'uppercase', letterSpacing: '0.1em', marginBottom: 12 }}>
              Featured agent
            </div>
            <h2 style={{ fontSize: 28, fontWeight: 700, letterSpacing: '-0.02em',
              marginBottom: 16, lineHeight: 1.2 }}>
              Financial Research Agent
            </h2>
            <p style={{ fontSize: 15, color: 'var(--text-secondary)', lineHeight: 1.7,
              marginBottom: 24 }}>
              Fetches the most recent SEC 10-K or 10-Q filing for any public company
              and returns a structured investment brief — signal, highlights, risks —
              synthesized by an LLM. Ready to call right now.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {[
                'Any NYSE or NASDAQ ticker',
                'Structured JSON output every time',
                'Full refund if the call fails',
                '90% of each fee goes to the agent',
              ].map(f => (
                <div key={f} style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                    stroke="var(--positive)" strokeWidth="2.5">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>{f}</span>
                </div>
              ))}
            </div>
          </motion.div>

          <motion.div {...fadeUp(0.1)}>
            <AgentPreviewCard />
          </motion.div>
        </div>
      </section>

      {/* Bottom CTA */}
      <section style={{ maxWidth: 1080, margin: '0 auto', padding: '80px 32px',
        textAlign: 'center' }}>
        <motion.div {...fadeUp(0)}>
          <h2 style={{ fontSize: 32, fontWeight: 700, letterSpacing: '-0.02em',
            marginBottom: 16 }}>
            Ready to try it?
          </h2>
          <p style={{ fontSize: 16, color: 'var(--text-secondary)', marginBottom: 32 }}>
            Start the server and open the dashboard — your first call takes about 30 seconds.
          </p>
          <button
            onClick={() => document.getElementById('connect-form').scrollIntoView({ behavior: 'smooth' })}
            style={{
              padding: '12px 28px', borderRadius: 'var(--radius-md)',
              background: 'var(--brand)', color: 'white',
              fontSize: 15, fontWeight: 500, cursor: 'pointer',
            }}
          >
            Connect to dashboard →
          </button>
        </motion.div>
      </section>

      {/* Footer */}
      <footer style={{ borderTop: '1px solid var(--border)', padding: '24px 32px' }}>
        <div style={{ maxWidth: 1080, margin: '0 auto',
          display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <span style={{ fontSize: 14, fontWeight: 600 }}>agentmarket</span>
          <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            Built with FastAPI · Groq · SQLite
          </span>
        </div>
      </footer>
    </div>
  )
}
