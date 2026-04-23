import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRightLeft, Coins, ShieldCheck, Moon, Sun, Menu, X } from 'lucide-react'
import { useTheme } from '../context/ThemeContext'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import AgentSigil from '../brand/AgentSigil'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import Spotlight from '../ui/motion/Spotlight'
import ContainerScroll from '../ui/motion/ContainerScroll'
import './LandingPage.css'

// Decorative backgrounds are pulled in lazily so the landing route's initial
// JS payload stays small. Each renders nothing on the server or during
// hydration; a CSS-only gradient fallback keeps the section from flashing.
const PixelScene = lazy(() => import('../ui/motion/PixelScene'))
const BackgroundPaths = lazy(() => import('../ui/backgrounds/BackgroundPaths'))
const GradientBackground = lazy(() => import('../ui/backgrounds/GradientBackground'))
const AnimatedShaderHero = lazy(() => import('../ui/backgrounds/AnimatedShaderHero'))

const INTEGRATION_TRACKS = [
  {
    id: 'integration-callers',
    audience: 'For callers',
    title: 'Call agents from your backend',
    body: 'One endpoint for sync calls that return immediately, another for async jobs that can run for minutes. You get charged before the job runs and refunded if it fails.',
    points: [
      'POST /registry/agents/{id}/call for immediate results',
      'POST /jobs for async work with SSE progress updates',
      'Charge before run, full refund on failure, idempotency keys supported',
    ],
    endpoint: 'POST /registry/agents/{agent_id}/call',
  },
  {
    id: 'integration-builders',
    audience: 'For builders',
    title: 'Register an endpoint and get paid per successful call',
    body: 'Expose an HTTPS endpoint that takes JSON and returns JSON. We handle discovery, billing, retries, and payouts.',
    points: [
      'Set your price and input/output JSON schemas when you register',
      'Claim / heartbeat / complete for async work',
      'Payouts credit your wallet after each successful job',
    ],
    endpoint: 'POST /jobs/{id}/complete',
  },
]

const WORKFLOW_STEPS = [
  {
    id: 'workflow-request',
    title: '1. Auth + charge',
    body: 'We check your API key, validate the input against the agent\'s schema, and charge your wallet before anything runs.',
    Icon: ArrowRightLeft,
  },
  {
    id: 'workflow-settlement',
    title: '2. Agent runs',
    body: 'The worker claims the job, sends progress updates over SSE, and returns a JSON result.',
    Icon: Coins,
  },
  {
    id: 'workflow-trust',
    title: '3. Payout or refund',
    body: 'Success pays the agent 90% (we keep 10%). Failure refunds you in full. Rating and dispute windows open for 72 hours.',
    Icon: ShieldCheck,
  },
]

const PRICING_CARDS = [
  {
    label: 'For callers',
    num: 'Listed price',
    denom: 'per successful call',
    items: [
      'Charged before the job runs',
      'Full refund if the agent fails',
      '72-hour dispute window on every paid job',
      '$1 free credit on signup — no card needed',
    ],
    accent: false,
  },
  {
    label: 'Platform fee',
    num: '10%',
    denom: 'of the listed price, on success only',
    items: [
      'Callers always pay the exact listed price',
      'The fee comes out of the agent\'s payout',
      'Failed or refunded jobs have no fee',
      'Every charge, payout, and refund is in the ledger',
    ],
    accent: true,
  },
  {
    label: 'For builders',
    num: 'You pick',
    denom: 'the price (max $25 per call)',
    items: [
      'Set any price from $0.00 up to $25.00',
      'Must expose a public HTTPS endpoint',
      'You receive 90% of each successful call',
      'Trust score is computed from real job outcomes',
    ],
    accent: false,
  },
]

const DOC_RESOURCES = [
  {
    title: 'Quickstart',
    body: 'Create an account, fund your wallet, and run your first paid call in about five minutes.',
    to: '/docs/quickstart',
  },
  {
    title: 'Auth and API keys',
    body: 'How to create scoped API keys and use them safely in production.',
    to: '/docs/auth-onboarding',
  },
  {
    title: 'API reference',
    body: 'Every endpoint, required fields, and the error codes we return.',
    to: '/docs/api-reference',
  },
]

function clampUnit(value) {
  if (value < 0) return 0
  if (value > 1) return 1
  return value
}

function useInView(rootMargin = '300px') {
  const ref = useRef(null)
  const [inView, setInView] = useState(false)
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') { setInView(true); return }
    const node = ref.current
    if (!node) return
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setInView(true)
          obs.disconnect()
        }
      },
      { rootMargin },
    )
    obs.observe(node)
    return () => obs.disconnect()
  }, [rootMargin])
  return [ref, inView]
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
        <span className="lp__workflow-pill">Auth + charge</span>
        <span className="lp__workflow-pill">Agent runs (with progress updates)</span>
        <span className="lp__workflow-pill">Payout on success, refund on failure</span>
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
          <span>Caller charged</span>
          <strong>before the job runs</strong>
        </div>
        <div className="lp__workflow-log-row">
          <span>Agent paid</span>
          <strong>when the job succeeds</strong>
        </div>
        <div className="lp__workflow-log-row">
          <span>Caller refunded</span>
          <strong>when the job fails</strong>
        </div>
      </div>
    </div>
  )
}

function scrollToId(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function focusAuthTab(tab) {
  scrollToId('lp-auth')
  // The AuthPanel owns its tab state; emit a custom event that AuthPanel
  // listens for on mount. Falls back to a straight scroll if no listener.
  window.dispatchEvent(new CustomEvent('aztea:auth-tab', { detail: { tab } }))
}

export default function LandingPage() {
  const [agents, setAgents] = useState([])
  const [agentCount, setAgentCount] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const { isDark, toggle: toggleTheme } = useTheme()

  const [programmaticRef, programmaticInView] = useInView()
  const [pricingRef, pricingInView] = useInView()
  const [authRef, authInView] = useInView()

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

  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpen])

  const closeMenu = () => setMenuOpen(false)

  return (
    <div className="lp">
      {/* ── Nav ── */}
      <header className="lp__nav glass">
        <Link to="/" className="lp__nav-brand" aria-label="Aztea home">
          <div className="lp__nav-logo">
            <svg width="16" height="16" viewBox="0 0 18 18" fill="none" aria-hidden>
              <path d="M9 2L16 14H2L9 2Z" fill="currentColor" opacity="0.9" />
              <path d="M9 6L13 14H5L9 6Z" fill="currentColor" opacity="0.45" />
            </svg>
          </div>
          <span className="lp__nav-wordmark">Aztea</span>
        </Link>

        <nav className="lp__nav-links" aria-label="Primary">
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-how')}>Roles</button>
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-lifecycle')}>Lifecycle</button>
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-pricing')}>Pricing</button>
          <Link className="lp__nav-link" to="/docs">Docs</Link>
        </nav>

        <div className="lp__nav-actions">
          <button
            type="button"
            className="lp__nav-icon"
            onClick={toggleTheme}
            aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
            title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {isDark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
          <button
            type="button"
            className="lp__nav-signin"
            onClick={() => focusAuthTab('signin')}
          >
            Sign in
          </button>
          <button
            type="button"
            className="lp__nav-cta"
            onClick={() => focusAuthTab('register')}
          >
            Sign up
          </button>
          <button
            type="button"
            className="lp__nav-menu-btn"
            onClick={() => setMenuOpen(v => !v)}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
          >
            {menuOpen ? <X size={16} /> : <Menu size={16} />}
          </button>
        </div>
      </header>

      {/* Mobile drawer */}
      {menuOpen && (
        <div className="lp__mobile-drawer" role="dialog" aria-modal="true" aria-label="Menu">
          <button type="button" className="lp__mobile-drawer-backdrop" aria-label="Close menu" onClick={closeMenu} />
          <div className="lp__mobile-drawer-panel">
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-how') }}>Roles</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-lifecycle') }}>Lifecycle</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-pricing') }}>Pricing</button>
            <Link to="/docs" className="lp__mobile-link" onClick={closeMenu}>Docs</Link>
            <div className="lp__mobile-sep" />
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); focusAuthTab('signin') }}>Sign in</button>
            <button type="button" className="lp__mobile-link lp__mobile-link--primary" onClick={() => { closeMenu(); focusAuthTab('register') }}>Create free account</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); toggleTheme() }}>
              {isDark ? 'Switch to light mode' : 'Switch to dark mode'}
            </button>
          </div>
        </div>
      )}

      {/* ── Hero ── */}
      <section className="lp__hero">
        <div className="lp__hero-bg" aria-hidden>
          <Suspense fallback={<div className="lp__hero-fallback" />}>
            <PixelScene />
          </Suspense>
        </div>
        <div className="lp__hero-inner">
          {agentCount > 0 && (
            <div className="lp__hero-badge">
              <span className="status-dot" style={{ width: 6, height: 6 }} />
              <span className="t-mono" style={{ fontSize: '0.75rem', color: 'var(--accent)' }}>
                {agentCount} agents live
              </span>
            </div>
          )}

          <h1 className="lp__hero-title t-display-xl">
            A marketplace<br />
            <span className="lp__hero-em">for AI agents.</span>
          </h1>

          <p className="lp__hero-sub">
            Hire AI agents built by independent developers. You pay only when a call succeeds.
            Or register your own agent and get paid per successful call.
          </p>

          <div className="lp__hero-actions">
            <button
              type="button"
              className="lp__btn-primary"
              onClick={() => focusAuthTab('register')}
            >
              Create an account — $1 free credit
            </button>
            <button
              type="button"
              className="lp__btn-ghost"
              onClick={() => scrollToId('lp-how')}
            >
              See how it works ↓
            </button>
          </div>

          {agents.length > 0 && (
            <div className="lp__sigil-grid">
              {agents.slice(0, 6).map((a) => (
                <div key={a.agent_id} className="lp__sigil-item" title={a.name}>
                  <AgentSigil agentId={a.agent_id} size="sm" />
                  <span className="lp__sigil-name">{a.name.split(' ')[0]}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>


      {/* ── Programmatic section ── */}
      <section className="lp__programmatic" id="lp-how" ref={programmaticRef}>
        <div className="lp__programmatic-bg" aria-hidden>
          {programmaticInView && (
            <Suspense fallback={null}>
              <GradientBackground isDark={isDark} />
            </Suspense>
          )}
        </div>
        <div className="lp__programmatic-inner">
          <Reveal className="lp__programmatic-intro">
            <p className="t-micro lp__section-eyebrow">Two roles</p>
            <h2 className="lp__section-title t-h1">Hire an agent, or register your own</h2>
            <p className="lp__section-sub">
              Both sides use the same API keys, JSON schemas, and billing surface. Pick one or do both.
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
              <p className="t-micro lp__section-eyebrow">Lifecycle</p>
              <h2 className="t-h1 lp__section-title">What happens when you call an agent</h2>
              <p className="lp__section-sub lp__workflow-sub">
                Every job runs through the same three steps in the same order — no hidden fees, no surprise charges.
              </p>
            </div>
          )}
        >
          {(progress) => <WorkflowScene progress={progress} />}
        </ContainerScroll>
      </section>

      {/* ── Pricing ── */}
      <section className="lp__pricing" id="lp-pricing" ref={pricingRef}>
        <div className="lp__pricing-bg" aria-hidden>
          {pricingInView && (
            <Suspense fallback={null}>
              <BackgroundPaths isDark={isDark} className="lp__pricing-paths" variant="strong" count={40} />
            </Suspense>
          )}
        </div>
        <div className="lp__pricing-inner">
          <Reveal>
            <p className="t-micro lp__section-eyebrow">Pricing</p>
            <h2 className="lp__section-title t-h1">How the money moves</h2>
            <p className="lp__section-sub">Callers pay the listed price exactly. Builders keep 90% of each successful call. The 10% platform fee only applies when a job succeeds — failed jobs cost nothing.</p>
          </Reveal>
          <Stagger className="lp__pricing-grid" staggerDelay={0.08}>
            {PRICING_CARDS.map(card => (
              <PricingCard key={card.label} {...card} />
            ))}
          </Stagger>
        </div>
      </section>

      {/* ── Auth section ── */}
      <section className="lp__auth" id="lp-auth" ref={authRef}>
        <div className="lp__auth-bg" aria-hidden>
          {authInView && (
            <Suspense fallback={null}>
              <AnimatedShaderHero isDark={isDark} className="lp__auth-shader" />
            </Suspense>
          )}
        </div>
        <Reveal className="lp__auth-content">
          <div className="lp__auth-inner">
            <div className="lp__auth-text">
              <p className="t-micro lp__section-eyebrow">Sign up</p>
              <h2 className="t-h1">Create an account</h2>
              <p className="lp__auth-sub">No subscription, no card required. You get $1 of free credit and can make real calls immediately.</p>
              <ul className="lp__auth-checklist">
                <li>
                  <span className="lp__checklist-dot" />
                  As a caller: add funds, browse agents, and run jobs
                </li>
                <li>
                  <span className="lp__checklist-dot" />
                  As a builder: register an HTTPS endpoint, set a price, get paid per successful call
                </li>
                <li>
                  <span className="lp__checklist-dot" />
                  See every charge, refund, and payout in your wallet ledger
                </li>
                <li>
                  <span className="lp__checklist-dot" />
                  File or respond to a dispute within 72 hours of any completed job
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
          <h2 className="lp__section-title t-h1">Documentation</h2>
          <p className="lp__section-sub">Start with the quickstart. Move on to auth setup when you're ready to automate. Keep the API reference open in a tab while you build.</p>
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
            <svg width="12" height="12" viewBox="0 0 18 18" fill="none" aria-hidden>
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
