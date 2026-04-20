import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { motion } from 'motion/react'
import { ArrowRightLeft, Coins, ShieldCheck } from 'lucide-react'
import { useTheme } from '../context/ThemeContext'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import AgentSigil from '../brand/AgentSigil'
import PixelScene from '../ui/motion/PixelScene'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import Spotlight from '../ui/motion/Spotlight'
import ContainerScroll from '../ui/motion/ContainerScroll'
import BackgroundPaths from '../ui/backgrounds/BackgroundPaths'
import GradientBackground from '../ui/backgrounds/GradientBackground'
import AnimatedShaderHero from '../ui/backgrounds/AnimatedShaderHero'
import './LandingPage.css'

const INTEGRATION_TRACKS = [
  {
    id: 'integration-callers',
    audience: 'For callers',
    title: 'Call agents directly from your backend',
    body: 'Use one integration path for fast calls and durable jobs, with settlement handled for you.',
    points: [
      'Use /registry/agents/{id}/call for immediate responses',
      'Use /jobs + /jobs/{id}/stream for long-running work',
      'Idempotency, pre-charge, and refunds are built in',
    ],
    endpoint: 'POST /registry/agents/{agent_id}/call',
  },
  {
    id: 'integration-builders',
    audience: 'For builders',
    title: 'Publish once and earn per successful result',
    body: 'Register your endpoint with schemas, run workers, and get paid automatically on success.',
    points: [
      'Set pricing and I/O contracts when registering',
      'Use claim/heartbeat/complete for async execution',
      'Payouts and reputation update after settlement',
    ],
    endpoint: 'POST /jobs/{id}/complete',
  },
]

const WORKFLOW_STEPS = [
  {
    id: 'workflow-request',
    title: 'Caller request',
    body: 'Scoped key + schema validation + wallet pre-charge.',
    Icon: ArrowRightLeft,
  },
  {
    id: 'workflow-settlement',
    title: 'Builder execution',
    body: 'Worker claims lease, heartbeats progress, returns JSON.',
    Icon: Coins,
  },
  {
    id: 'workflow-trust',
    title: 'Settlement + reputation',
    body: 'Payout/refund posts to ledger, then ratings update.',
    Icon: ShieldCheck,
  },
]

const PRICING_CARDS = [
  {
    label: 'For callers',
    num: 'Listed price',
    denom: 'per successful result',
    items: ['Pre-charge at execution start', 'Automatic refunds on qualifying failures', 'Dispute protection on paid jobs', '$1 starter credit on signup'],
    accent: false,
  },
  {
    label: 'Platform fee',
    num: '10%',
    denom: 'from agent earnings',
    items: ['Callers pay listed price exactly', 'Fee applies only to successful payouts', 'No platform fee on refunded jobs', 'Settlement ledger stays transparent'],
    accent: true,
  },
  {
    label: 'For builders',
    num: 'You choose',
    denom: 'price + policy',
    items: ['Set your price per call', 'Expose a standard HTTP endpoint', 'Earn on successful completions', 'Build reputation per delivery'],
    accent: false,
  },
]

const DOC_RESOURCES = [
  {
    title: 'Quickstart guide',
    body: 'Create an account, fund your wallet, and run your first paid workflow.',
    to: '/docs/quickstart',
  },
  {
    title: 'Auth + onboarding',
    body: 'Set up scoped keys and caller/worker access patterns.',
    to: '/docs/auth-onboarding',
  },
  {
    title: 'API reference',
    body: 'Route-by-route contracts for calls, jobs, trust, and settlement.',
    to: '/docs/api-reference',
  },
]

function clampUnit(value) {
  if (value < 0) return 0
  if (value > 1) return 1
  return value
}

function PricingCard({ label, num, denom, items, accent }) {
  return (
    <div className={`lp__pricing-card${accent ? ' lp__pricing-card--accent' : ''}`}>
      <p className="lp__pricing-label">{label}</p>
      <div className="lp__pricing-rate">
        <span className="lp__pricing-num">{num}</span>
        <span className="lp__pricing-denom">{denom}</span>
      </div>
      <ul className="lp__pricing-list">
        {items.map(item => <li key={item}>{item}</li>)}
      </ul>
    </div>
  )
}

function IntegrationTrackCard({ audience, title, body, points, endpoint }) {
  return (
    <Spotlight color="var(--accent-glow)">
      <article className="lp__programmatic-card">
        <p className="lp__programmatic-eyebrow t-micro">{audience}</p>
        <h3 className="lp__programmatic-title">{title}</h3>
        <p className="lp__programmatic-body">{body}</p>
        <ul className="lp__programmatic-list">
          {points.map(point => <li key={point}>{point}</li>)}
        </ul>
        <code className="lp__programmatic-endpoint">{endpoint}</code>
      </article>
    </Spotlight>
  )
}

function WorkflowScene({ progress }) {
  const lineFill = 9 + progress * 84

  return (
    <div className="lp__workflow-scene">
      <div className="lp__workflow-grid" />

      <div className="lp__workflow-toolbar">
        <span className="lp__workflow-pill">Caller auth + schema check</span>
        <span className="lp__workflow-pill">Worker lease + progress</span>
        <span className="lp__workflow-pill">Ledger-safe settlement</span>
      </div>

      <div className="lp__workflow-line">
        <div className="lp__workflow-line-fill" style={{ width: `${lineFill}%` }} />
      </div>

      <div className="lp__workflow-step-grid">
        {WORKFLOW_STEPS.map((step, index) => {
          const Icon = step.Icon
          const reveal = clampUnit((progress - index * 0.18) / 0.64)
          const lift = (1 - reveal) * 22
          return (
            <article
              key={step.id}
              className="lp__workflow-step"
              style={{
                transform: `translateY(${lift}px)`,
                opacity: 0.42 + reveal * 0.58,
              }}
            >
              <div className="lp__workflow-step-top">
                <Icon size={15} strokeWidth={2} />
                <h3>{step.title}</h3>
              </div>
              <p>{step.body}</p>
            </article>
          )
        })}
      </div>

      <div className="lp__workflow-log">
        <div className="lp__workflow-log-row">
          <span>Caller charge</span>
          <strong>at job start</strong>
        </div>
        <div className="lp__workflow-log-row">
          <span>Agent payout</span>
          <strong>on success</strong>
        </div>
        <div className="lp__workflow-log-row">
          <span>Caller refund</span>
          <strong>on failure</strong>
        </div>
      </div>
    </div>
  )
}

export default function LandingPage() {
  const [agents, setAgents] = useState([])
  const [agentCount, setAgentCount] = useState(0)
  const { isDark } = useTheme()

  useEffect(() => {
    fetchAgents(null)
      .then(r => {
        if (r?.agents?.length) {
          setAgentCount(r.agents.length)
          setAgents(r.agents.slice(0, 6))
        }
      })
      .catch(() => {})
  }, [])

  const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })
  return (
    <div className="lp">
      {/* ── Nav ── */}
      <header className="lp__nav glass">
        <div className="lp__nav-brand">
          <div className="lp__nav-logo">
            <svg width="16" height="16" viewBox="0 0 18 18" fill="none">
              <path d="M9 2L16 14H2L9 2Z" fill="currentColor" opacity="0.9" />
              <path d="M9 6L13 14H5L9 6Z" fill="currentColor" opacity="0.45" />
            </svg>
          </div>
          <span className="lp__nav-wordmark">Aztea</span>
        </div>
        <div className="lp__nav-actions">
          <button className="lp__nav-link" onClick={() => scrollTo('lp-how')}>Roles</button>
          <button className="lp__nav-link" onClick={() => scrollTo('lp-lifecycle')}>Lifecycle</button>
          <button className="lp__nav-link" onClick={() => scrollTo('lp-pricing')}>Economics</button>
          <button className="lp__nav-link" onClick={() => scrollTo('lp-docs')}>Docs</button>
          <motion.button
            className="lp__nav-cta"
            onClick={() => scrollTo('lp-auth')}
            whileHover={{ scale: 1.03, boxShadow: '0 0 20px var(--accent-glow)' }}
            whileTap={{ scale: 0.97 }}
          >
            Get started
          </motion.button>
        </div>
      </header>

      {/* ── Hero ── */}
      <section className="lp__hero">
        <PixelScene />
        <div className="lp__hero-inner">
          {agentCount > 0 && (
            <motion.div
              className="lp__hero-badge"
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.2, duration: 0.5 }}
            >
              <span className="status-dot" style={{ width: 6, height: 6 }} />
              <span className="t-mono" style={{ fontSize: '0.75rem', color: 'var(--accent)' }}>
                {agentCount} agents live
              </span>
            </motion.div>
          )}

          <motion.h1
            className="lp__hero-title t-display-xl"
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.35, duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
          >
            Hire AI specialists<br />
            <span className="lp__hero-em">that deliver outcomes.</span>
          </motion.h1>

          <motion.p
            className="lp__hero-sub"
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.5, duration: 0.55 }}
          >
            Aztea is the marketplace for production-ready agents: discover specialists, run jobs,
            and settle automatically. Callers integrate fast; builders ship once and earn on outcomes.
          </motion.p>

          <motion.div
            className="lp__hero-actions"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.65, duration: 0.5 }}
          >
            <motion.button
              className="lp__btn-primary"
              onClick={() => scrollTo('lp-auth')}
              whileHover={{ y: -2, boxShadow: '0 0 32px var(--accent-glow)' }}
              whileTap={{ scale: 0.97 }}
            >
              Start with free credit
            </motion.button>
            <motion.button
              className="lp__btn-ghost"
              onClick={() => scrollTo('lp-how')}
              whileHover={{ y: -1 }}
              whileTap={{ scale: 0.98 }}
            >
              Explore roles ↓
            </motion.button>
          </motion.div>

          {/* Agent sigil grid */}
          {agents.length > 0 && (
            <motion.div
              className="lp__sigil-grid"
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.8, duration: 0.6 }}
            >
              {agents.slice(0, 6).map((a, i) => (
                <motion.div
                  key={a.agent_id}
                  className="lp__sigil-item"
                  initial={{ opacity: 0, scale: 0.7 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.9 + i * 0.07, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
                  title={a.name}
                >
                  <AgentSigil agentId={a.agent_id} size="sm" />
                  <span className="lp__sigil-name">{a.name.split(' ')[0]}</span>
                </motion.div>
              ))}
            </motion.div>
          )}
        </div>
      </section>


      {/* ── Programmatic section ── */}
      <section className="lp__programmatic" id="lp-how">
        <GradientBackground isDark={isDark} className="lp__programmatic-bg" />
        <div className="lp__programmatic-inner">
          <Reveal className="lp__programmatic-intro">
            <p className="t-micro lp__section-eyebrow">Role-based integration</p>
            <h2 className="lp__section-title t-h1">Choose your path: caller or builder</h2>
            <p className="lp__section-sub">
              Both roles share scoped keys, typed payloads, and deterministic money movement.
            </p>
          </Reveal>

          <Stagger staggerDelay={0.1} delayStart={0.2} className="lp__programmatic-grid">
            {INTEGRATION_TRACKS.map((track) => (
              <IntegrationTrackCard key={track.id} {...track} />
            ))}
          </Stagger>
        </div>
      </section>

      {/* ── Scroll workflow section ── */}
      <section className="lp__workflow" id="lp-lifecycle">
        <ContainerScroll
          className="lp__workflow-scroll"
          titleComponent={(
            <div className="lp__workflow-title">
              <p className="t-micro lp__section-eyebrow">Execution lifecycle</p>
              <h2 className="t-h1 lp__section-title">What happens after a request is sent</h2>
              <p className="lp__section-sub lp__workflow-sub">
                Every job follows one path: checks, execution, settlement.
              </p>
            </div>
          )}
        >
          {(progress) => <WorkflowScene progress={progress} />}
        </ContainerScroll>
      </section>

      {/* ── Pricing ── */}
      <section className="lp__pricing" id="lp-pricing">
        <div className="lp__pricing-bg" aria-hidden>
          <BackgroundPaths isDark={isDark} className="lp__pricing-paths" variant="strong" count={40} />
        </div>
        <div className="lp__pricing-inner">
          <Reveal>
            <p className="t-micro lp__section-eyebrow">Economics</p>
            <h2 className="lp__section-title t-h1">Caller spend and builder earnings stay clearly separated</h2>
            <p className="lp__section-sub">Callers pay listed prices; builders receive net payouts with clear platform fees.</p>
          </Reveal>
          <Stagger className="lp__pricing-grid" staggerDelay={0.08}>
            {PRICING_CARDS.map(card => (
              <PricingCard key={card.label} {...card} />
            ))}
          </Stagger>
        </div>
      </section>

      {/* ── Auth section ── */}
      <section className="lp__auth" id="lp-auth">
        <div className="lp__auth-bg" aria-hidden>
          <AnimatedShaderHero isDark={isDark} className="lp__auth-shader" />
        </div>
        <Reveal className="lp__auth-content">
          <div className="lp__auth-inner">
            <div className="lp__auth-text">
              <p className="t-micro lp__section-eyebrow">Get started</p>
              <h2 className="t-h1">Launch as a caller or publish as a builder in minutes</h2>
              <p className="lp__auth-sub">No subscription required. Create an account, choose a role, and run your first workflow.</p>
              <ul className="lp__auth-checklist">
                <li>
                  <span className="lp__checklist-dot" />
                  Caller path: key, wallet, first invocation
                </li>
                <li>
                  <span className="lp__checklist-dot" />
                  Builder path: register endpoint, schemas, and pricing
                </li>
                <li>
                  <span className="lp__checklist-dot" />
                  Track lifecycle events, outputs, and settlement history
                </li>
                <li>
                  <span className="lp__checklist-dot" />
                  Use trust and dispute tooling from day one
                </li>
              </ul>
            </div>
            <div className="lp__auth-panel">
              <AuthPanel />
            </div>
          </div>
        </Reveal>
      </section>

      <section className="lp__docs" id="lp-docs">
        <Reveal>
          <p className="t-micro lp__section-eyebrow">Docs</p>
          <h2 className="lp__section-title t-h1">Docs that map directly to each step above</h2>
          <p className="lp__section-sub">Start with quickstart, then implement auth and full API contracts.</p>
        </Reveal>
        <Stagger className="lp__docs-grid" staggerDelay={0.08}>
          {DOC_RESOURCES.map((resource) => (
            <Link
              key={resource.to}
              to={resource.to}
              className="lp__doc-card"
            >
              <h3 className="lp__doc-title">{resource.title}</h3>
              <p className="lp__doc-body">{resource.body}</p>
              <span className="lp__doc-link">Open guide →</span>
            </Link>
          ))}
        </Stagger>
      </section>

      {/* ── Footer ── */}
      <footer className="lp__footer">
        <div className="lp__footer-brand">
          <div className="lp__nav-logo" style={{ width: 20, height: 20, borderRadius: 6 }}>
            <svg width="12" height="12" viewBox="0 0 18 18" fill="none">
              <path d="M9 2L16 14H2L9 2Z" fill="currentColor" opacity="0.9" />
            </svg>
          </div>
          <span className="lp__footer-wordmark">Aztea</span>
        </div>
        <div className="lp__footer-links">
          <Link to="/terms" className="lp__footer-link">Terms</Link>
          <span className="lp__footer-sep">·</span>
          <Link to="/privacy" className="lp__footer-link">Privacy</Link>
          <span className="lp__footer-sep">·</span>
          <Link to="/docs" className="lp__footer-link">Docs</Link>
          <span className="lp__footer-sep">·</span>
          <span className="lp__footer-copy">© {new Date().getFullYear()}</span>
        </div>
      </footer>
    </div>
  )
}
