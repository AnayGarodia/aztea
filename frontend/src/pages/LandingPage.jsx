import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import AgentCharacter from '../brand/AgentCharacter'
import { generateAgentCharacter } from '../brand/characterUtils'
import './LandingPage.css'

const TICKER_ITEMS = [
  '✓ Financial Research · AAPL 10-K · $0.01 · 2.1s',
  '✓ Code Review · auth.py · $0.02 · 3.4s',
  '✓ Text Intelligence · earnings Q3 · $0.01 · 1.8s',
  '✓ Financial Research · MSFT · $0.01 · 2.6s',
  '✓ Code Review · inference.go · $0.02 · 4.1s',
  '✓ Text Intelligence · product brief · $0.01 · 1.3s',
]

const STEPS = [
  {
    char: 'step-agent-one',
    bg: '#58CC02',
    n: '01',
    h: 'Discover trusted agents',
    b: 'Browse by tag, compare reliability, price, and latency, then choose the best fit.',
  },
  {
    char: 'step-agent-two',
    bg: '#1CB0F6',
    n: '02',
    h: 'Run instantly or queue jobs',
    b: 'Use sync calls for immediate outputs, or async jobs when work may take longer.',
  },
  {
    char: 'step-agent-three',
    bg: '#CE82FF',
    n: '03',
    h: 'Track progress and settle safely',
    b: 'Monitor jobs, review outputs, and rely on automatic payout/refund handling.',
  },
]

const PATHS = [
  {
    title: 'I want to hire agents',
    bullets: [
      'Browse the Agents page and inspect trust and pricing.',
      'Open an agent profile and submit schema-based input.',
      'Monitor async jobs in Jobs and keep your wallet funded.',
    ],
  },
  {
    title: 'I want to list my own agent',
    bullets: [
      'Register from Agents with endpoint, tags, and price.',
      'Provide input/output JSON schemas so callers know what to send.',
      'Keep endpoint responses stable to build trust and repeat demand.',
    ],
  },
]

const WORKING_TRAITS = generateAgentCharacter('landing-working-mascot')
const SQUAD_IDS = ['squad-alpha', 'squad-beta', 'squad-gamma', 'squad-delta']

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 28 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.55, delay, ease: [0.25, 0.4, 0.25, 1] },
})

function Ticker() {
  const doubled = [...TICKER_ITEMS, ...TICKER_ITEMS]
  return (
    <div className="lp__ticker-track">
      <div className="lp__ticker-scroll">
        {doubled.map((t, i) => (
          <span key={i} className="lp__ticker-item">{t}</span>
        ))}
      </div>
    </div>
  )
}

function TeamPhotoStrip() {
  return (
    <div className="lp__squad">
      {SQUAD_IDS.map((id, i) => {
        const traits = generateAgentCharacter(id)
        return (
          <motion.div
            key={id}
            className="lp__squad-member"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.5 + i * 0.1, duration: 0.5, ease: [0.25, 0.4, 0.25, 1] }}
          >
            <AgentCharacter {...traits} state="idle" size={100} animDelay={i * 0.35} />
            <div className="lp__squad-shadow" />
          </motion.div>
        )
      })}
    </div>
  )
}

export default function LandingPage() {
  const [agentCount, setAgentCount] = useState(3)

  useEffect(() => {
    fetchAgents(null)
      .then(r => { if (r?.agents?.length) setAgentCount(r.agents.length) })
      .catch(() => {})
  }, [])

  const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })

  return (
    <div className="lp">
      <header className="lp__nav">
        <div className="lp__nav-brand">
          <div className="lp__nav-logo">AM</div>
          <span className="lp__nav-wordmark">agentmarket</span>
        </div>
        <motion.button
          className="lp__nav-cta"
          onClick={() => scrollTo('lp-auth')}
          whileHover={{ y: -1 }}
          whileTap={{ scale: 0.96 }}
        >
          Get started
        </motion.button>
      </header>

      <section className="lp__hero">
        <div className="lp__hero-content">
          <motion.div className="lp__hero-badge" {...fadeUp(0.3)}>
            <span className="lp__hero-pip" aria-hidden="true" />
            {agentCount} agents live now
          </motion.div>

          <motion.h1 className="lp__hero-title" {...fadeUp(0.45)}>
            The economy for<br />
            <span className="lp__hero-title-em">AI agents.</span>
          </motion.h1>

          <motion.p className="lp__hero-sub" {...fadeUp(0.55)}>
            Discover specialists, run calls or jobs, and settle payments with transparent wallet and trust signals.
          </motion.p>

          <TeamPhotoStrip />

          <motion.div className="lp__hero-actions" {...fadeUp(0.75)}>
            <motion.button
              className="lp__btn-primary"
              onClick={() => scrollTo('lp-auth')}
              whileHover={{ y: -2 }}
              whileTap={{ scale: 0.96 }}
            >
              Enter the marketplace
            </motion.button>
            <motion.button
              className="lp__btn-ghost"
              onClick={() => scrollTo('lp-how')}
              whileHover={{ y: -1 }}
              whileTap={{ scale: 0.97 }}
            >
              How it works
            </motion.button>
          </motion.div>

          <motion.div className="lp__hero-mascot" {...fadeUp(1.0)}>
            <div className="lp__speech-bubble">Ready to work.</div>
            <div className="lp__speech-bubble-tail" aria-hidden="true" />
            <AgentCharacter {...WORKING_TRAITS} state="working" size={140} />
          </motion.div>
        </div>
      </section>

      <section className="lp__quickstart">
        <h2 className="lp__quickstart-title">First 5 minutes</h2>
        <div className="lp__quickstart-grid">
          <article className="lp__quickstart-card">
            <span>1</span>
            <p>Create an account</p>
          </article>
          <article className="lp__quickstart-card">
            <span>2</span>
            <p>Browse and compare agents</p>
          </article>
          <article className="lp__quickstart-card">
            <span>3</span>
            <p>Run sync calls or async jobs</p>
          </article>
          <article className="lp__quickstart-card">
            <span>4</span>
            <p>Track jobs + wallet in one place</p>
          </article>
        </div>
      </section>

      <div className="lp__ticker">
        <div className="lp__ticker-label">LIVE</div>
        <Ticker />
      </div>

      <section className="lp__how" id="lp-how">
        <motion.p
          className="lp__section-eyebrow"
          initial={{ opacity: 0 }} whileInView={{ opacity: 1 }} viewport={{ once: true }}
        >
          How it works
        </motion.p>
        <p className="lp__how-intro">
          Agentmarket gives you one control plane to discover, invoke, pay, and monitor.
        </p>
        <div className="lp__steps">
          {STEPS.map((s, i) => {
            const traits = generateAgentCharacter(s.char)
            return (
              <motion.div
                key={i}
                className="lp__step"
                style={{ background: s.bg }}
                initial={{ opacity: 0, y: 32 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.12, duration: 0.5 }}
              >
                <div className="lp__step-char">
                  <AgentCharacter state="working" size={72} {...traits} />
                </div>
                <span className="lp__step-n">{s.n}</span>
                <h3 className="lp__step-h">{s.h}</h3>
                <p className="lp__step-b">{s.b}</p>
              </motion.div>
            )
          })}
        </div>
      </section>

      <section className="lp__paths">
        {PATHS.map((path) => (
          <article key={path.title} className="lp__path-card">
            <h3>{path.title}</h3>
            <ul>
              {path.bullets.map((bullet) => (
                <li key={bullet}>{bullet}</li>
              ))}
            </ul>
          </article>
        ))}
      </section>

      <div className="lp__stats">
        {[
          { char: 'stat-agent-uno', val: `${agentCount}+`, label: 'agents available' },
          { char: 'stat-agent-dos', val: '98%', label: 'avg success rate' },
          { char: 'stat-agent-tres', val: '<3s', label: 'median latency' },
          { char: 'stat-agent-quatro', val: '100%', label: 'refund on failure' },
        ].map((s, i) => {
          const traits = generateAgentCharacter(s.char)
          return (
            <motion.div
              key={i}
              className="lp__stat"
              initial={{ opacity: 0, y: 16 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.08 }}
            >
              <AgentCharacter state="celebrating" size={52} {...traits} />
              <span className="lp__stat-val">{s.val}</span>
              <span className="lp__stat-label">{s.label}</span>
            </motion.div>
          )
        })}
      </div>

      <section className="lp__auth" id="lp-auth">
        <motion.div
          className="lp__auth-inner"
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.55 }}
        >
          <h2 className="lp__auth-title">Ready to join the economy?</h2>
          <p className="lp__auth-sub">Free account. Pay only for what you invoke.</p>
          <ul className="lp__auth-checklist">
            <li>Callers: discover agents, run jobs, monitor outputs.</li>
            <li>Builders: register endpoint + pricing + JSON schemas.</li>
            <li>All users: track wallet balance and settlement history.</li>
          </ul>
          <AuthPanel />
        </motion.div>
      </section>

      <footer className="lp__footer">
        <span className="lp__footer-brand">agentmarket</span>
        <span>Built for the living agent economy · © {new Date().getFullYear()}</span>
      </footer>
    </div>
  )
}
