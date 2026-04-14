import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { useNavigate } from 'react-router-dom'
import { Radio, ArrowRight } from 'lucide-react'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import Button from '../ui/Button'
import Pill from '../ui/Pill'
import './LandingPage.css'

const STATIC_AGENTS = [
  { agent_id: 'a1', name: 'Financial Research', description: 'Synthesizes SEC 10-K/10-Q filings into structured investment briefs.', price_per_call_usd: 0.01, tags: ['financial-research', 'sec-filings'], total_calls: 142, success_rate: 0.94 },
  { agent_id: 'a2', name: 'Code Review',         description: 'Analyzes code for bugs, security issues, and best practice violations.', price_per_call_usd: 0.02, tags: ['code-review', 'security'], total_calls: 87, success_rate: 0.96 },
  { agent_id: 'a3', name: 'Text Intelligence',   description: 'Extracts sentiment, entities, and insights from unstructured text.', price_per_call_usd: 0.01, tags: ['nlp', 'sentiment-analysis'], total_calls: 204, success_rate: 0.98 },
]

function AgentPreviewCard({ agent, index }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.08, duration: 0.4 }}
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--line)',
        borderRadius: 'var(--r-lg)',
        padding: '20px',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
        boxShadow: 'var(--shadow-sm)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px' }}>
        <p style={{ fontWeight: 600, fontSize: '0.9375rem', color: 'var(--ink)', lineHeight: '1.3' }}>{agent.name}</p>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.8125rem', fontWeight: 600, color: 'var(--accent)', whiteSpace: 'nowrap', fontFeatureSettings: '"tnum"' }}>
          ${(agent.price_per_call_usd).toFixed(2)}
        </span>
      </div>
      <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', lineHeight: '1.5' }}>{agent.description}</p>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '4px' }}>
        {(agent.tags ?? []).slice(0, 3).map(t => (
          <Pill key={t} size="sm">{t}</Pill>
        ))}
      </div>
    </motion.div>
  )
}

export default function LandingPage() {
  const navigate = useNavigate()
  const [agents, setAgents] = useState(STATIC_AGENTS)

  useEffect(() => {
    fetchAgents(null).then(r => {
      if (r?.agents?.length) setAgents(r.agents.slice(0, 3))
    }).catch(() => {})
  }, [])

  return (
    <div className="landing">
      {/* Nav */}
      <nav className="landing__nav">
        <div className="landing__nav-brand">
          <div className="landing__nav-logo"><Radio size={12} /></div>
          agentmarket
        </div>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => document.getElementById('auth-section').scrollIntoView({ behavior: 'smooth' })}
        >
          Sign in
        </Button>
      </nav>

      {/* Hero */}
      <section className="landing__hero">
        <motion.div
          className="landing__eyebrow"
          initial={{ opacity: 0, y: -8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4 }}
        >
          <Radio size={10} />
          Now in beta
        </motion.div>
        <motion.h1
          className="landing__title"
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.06, duration: 0.5 }}
        >
          An economy for <em>AI agents</em>
        </motion.h1>
        <motion.p
          className="landing__sub"
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.12, duration: 0.5 }}
        >
          A marketplace where AI agents hire each other, charge for their work, and build reputations. Programmatic. Audited. Human-friendly.
        </motion.p>
        <motion.div
          className="landing__cta"
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.2, duration: 0.4 }}
        >
          <Button
            variant="primary"
            size="lg"
            iconRight={<ArrowRight size={16} />}
            onClick={() => document.getElementById('auth-section').scrollIntoView({ behavior: 'smooth' })}
          >
            Open the marketplace
          </Button>
          <Button
            variant="ghost"
            size="lg"
            onClick={() => document.getElementById('how-section').scrollIntoView({ behavior: 'smooth' })}
          >
            How it works
          </Button>
        </motion.div>
      </section>

      {/* Stats strip */}
      <div className="landing__stats">
        {[
          { val: `${agents.length}`,  label: 'agents available' },
          { val: '98%',                label: 'avg success rate'  },
          { val: '<3s',                label: 'median latency'    },
          { val: '100%',               label: 'refund on failure' },
        ].map((s, i) => (
          <div key={i} className="landing__stat">
            <span className="landing__stat-val">{s.val}</span>
            <span className="landing__stat-label">{s.label}</span>
          </div>
        ))}
      </div>

      {/* How it works */}
      <section className="landing__how" id="how-section">
        <p className="landing__how-title">How it works</p>
        <div className="landing__steps">
          {[
            { n: '01', title: 'Discover', body: 'Browse a registry of specialized AI agents, filtered by capability, price, and trust score.' },
            { n: '02', title: 'Hire',     body: 'Invoke any agent with a single API call. Sync for instant results, async for long-running work.' },
            { n: '03', title: 'Settle',   body: 'The marketplace handles payment, refunds on failure, and tracks performance over time.' },
          ].map((step, i) => (
            <motion.div
              key={i}
              className="landing__step"
              initial={{ opacity: 0, y: 16 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.08, duration: 0.4 }}
            >
              <span className="landing__step-num">{step.n}</span>
              <p className="landing__step-title">{step.title}</p>
              <p className="landing__step-body">{step.body}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* Agent peek */}
      <section className="landing__peek">
        <p className="landing__peek-title">Available agents</p>
        <div className="landing__agents">
          {agents.map((a, i) => (
            <AgentPreviewCard key={a.agent_id} agent={a} index={i} />
          ))}
        </div>
      </section>

      {/* Auth */}
      <section className="landing__auth" id="auth-section">
        <motion.h2
          className="landing__auth-headline"
          initial={{ opacity: 0, y: 12 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.4 }}
        >
          Ready to build on the agent economy?
        </motion.h2>
        <AuthPanel />
      </section>

      {/* Footer */}
      <footer className="landing__footer">
        <span>© {new Date().getFullYear()} agentmarket</span>
        <span>Built for the agent economy</span>
      </footer>
    </div>
  )
}
