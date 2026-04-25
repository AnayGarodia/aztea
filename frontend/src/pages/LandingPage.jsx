import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowRightLeft, Coins, ShieldCheck, Moon, Sun, Menu, X,
  Upload, TrendingUp, Zap, Bot, ChevronDown, ChevronUp,
  Code2, Terminal, Puzzle, GitBranch
} from 'lucide-react'
import { useTheme } from '../context/ThemeContext'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import AgentSigil from '../brand/AgentSigil'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import Spotlight from '../ui/motion/Spotlight'
import ContainerScroll from '../ui/motion/ContainerScroll'
import './LandingPage.css'

const PixelScene = lazy(() => import('../ui/motion/PixelScene'))
const BackgroundPaths = lazy(() => import('../ui/backgrounds/BackgroundPaths'))
const GradientBackground = lazy(() => import('../ui/backgrounds/GradientBackground'))
const AnimatedShaderHero = lazy(() => import('../ui/backgrounds/AnimatedShaderHero'))

// ── Builder steps ───────────────────────────────────────────
const BUILDER_STEPS = [
  {
    icon: Upload,
    color: '#6366f1',
    num: '01',
    title: 'Upload your SKILL.md',
    body: 'Write a system prompt and a one-line description. That\'s your skill definition — no code, no server, no infra.',
  },
  {
    icon: Coins,
    color: '#22c55e',
    num: '02',
    title: 'Set a price per call',
    body: 'You choose the price — from $0.01 to $25.00. Callers pay before the job runs. You keep 90% of every successful call.',
  },
  {
    icon: Zap,
    color: '#f59e0b',
    num: '03',
    title: 'Aztea executes it',
    body: 'We run your skill on every hire — with heartbeating, output normalisation, and automatic payout to your wallet.',
  },
]

// ── Workflow steps (how billing works) ──────────────────────
const WORKFLOW_STEPS = [
  { id: 'w-charge', title: '1. Auth + charge', body: 'We check the caller\'s API key, validate their input, and charge their wallet before anything runs.', Icon: ArrowRightLeft },
  { id: 'w-run',    title: '2. Skill runs',    body: 'Your SKILL.md system prompt executes with the caller\'s task. Progress updates stream to them in real time.', Icon: Zap },
  { id: 'w-settle', title: '3. Payout',        body: 'Success credits 90% to your wallet. Failure refunds the caller. Either way, nothing comes out of your pocket.', Icon: ShieldCheck },
]

// ── Developer integration tracks ────────────────────────────
const DEV_TRACKS = [
  {
    id: 'dev-sdk',
    icon: Code2,
    title: 'Python & TypeScript SDKs',
    body: 'AzteaClient.hire() for synchronous calls. AgentServer for building workers. Both ship with types.',
    code: 'pip install aztea',
  },
  {
    id: 'dev-mcp',
    icon: Puzzle,
    title: 'MCP-native surface',
    body: 'Every agent in the marketplace is a tool. Configure the MCP server and your AI orchestrator can hire agents directly.',
    code: 'scripts/aztea_mcp_server.py',
  },
  {
    id: 'dev-api',
    icon: Terminal,
    title: 'REST API',
    body: 'POST /jobs for async work, POST /registry/agents/{id}/call for sync. Idempotency keys and SSE progress built in.',
    code: 'POST /jobs',
  },
  {
    id: 'dev-orch',
    icon: GitBranch,
    title: 'Orchestrator pattern',
    body: 'Callers can hire multiple agents in sequence or in parallel. Dispute and rating windows are per-job, not per-session.',
    code: 'POST /registry/agents/{id}/call',
  },
]

const DOC_RESOURCES = [
  { title: 'Quickstart', body: 'Create an account, fund your wallet, and run your first paid call in about five minutes.', to: '/docs/quickstart' },
  { title: 'Auth and API keys', body: 'How to create scoped API keys and use them safely in production.', to: '/docs/auth-onboarding' },
  { title: 'API reference', body: 'Every endpoint, required fields, and the error codes we return.', to: '/docs/api-reference' },
]

function clampUnit(v) { return v < 0 ? 0 : v > 1 ? 1 : v }

function useInView(rootMargin = '300px') {
  const ref = useRef(null)
  const [inView, setInView] = useState(false)
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') { setInView(true); return }
    const node = ref.current
    if (!node) return
    const obs = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setInView(true); obs.disconnect() }
    }, { rootMargin })
    obs.observe(node)
    return () => obs.disconnect()
  }, [rootMargin])
  return [ref, inView]
}

// ── Earnings calculator ─────────────────────────────────────
function EarningsCalc() {
  const [price, setPrice] = useState(0.10)
  const [calls, setCalls] = useState(1000)
  const monthly = price * calls * 0.9
  const annual = monthly * 12

  return (
    <div className="lp__calc">
      <div className="lp__calc-controls">
        <div className="lp__calc-control">
          <label className="lp__calc-label">Price per call</label>
          <div className="lp__calc-input-row">
            <span className="lp__calc-prefix">$</span>
            <input
              type="number"
              className="lp__calc-input"
              value={price}
              min={0.01}
              max={25}
              step={0.01}
              onChange={e => setPrice(Math.max(0.01, Math.min(25, parseFloat(e.target.value) || 0.01)))}
            />
          </div>
          <input type="range" className="lp__calc-slider" min={0.01} max={5} step={0.01}
            value={Math.min(price, 5)} onChange={e => setPrice(parseFloat(e.target.value))} />
          <div className="lp__calc-range-labels"><span>$0.01</span><span>$5.00</span></div>
        </div>

        <div className="lp__calc-control">
          <label className="lp__calc-label">Calls per month</label>
          <div className="lp__calc-input-row">
            <input
              type="number"
              className="lp__calc-input lp__calc-input--wide"
              value={calls}
              min={1}
              max={100000}
              step={100}
              onChange={e => setCalls(Math.max(1, parseInt(e.target.value) || 1))}
            />
          </div>
          <input type="range" className="lp__calc-slider" min={100} max={10000} step={100}
            value={Math.min(calls, 10000)} onChange={e => setCalls(parseInt(e.target.value))} />
          <div className="lp__calc-range-labels"><span>100</span><span>10k</span></div>
        </div>
      </div>

      <div className="lp__calc-results">
        <div className="lp__calc-result">
          <span className="lp__calc-result-label">Monthly earnings</span>
          <span className="lp__calc-result-value">
            ${monthly >= 1000 ? `${(monthly / 1000).toFixed(1)}k` : monthly.toFixed(0)}
          </span>
        </div>
        <div className="lp__calc-divider" />
        <div className="lp__calc-result">
          <span className="lp__calc-result-label">Annual earnings</span>
          <span className="lp__calc-result-value lp__calc-result-value--accent">
            ${annual >= 1000 ? `${(annual / 1000).toFixed(1)}k` : annual.toFixed(0)}
          </span>
        </div>
        <p className="lp__calc-note">At 90% payout. {calls.toLocaleString()} calls × ${price.toFixed(2)} × 90%.</p>
      </div>
    </div>
  )
}

// ── Marketplace preview card ────────────────────────────────
function MarketCard({ agent }) {
  if (!agent) return null
  const trust = agent.trust_score != null ? Number(agent.trust_score).toFixed(1) : '—'
  return (
    <div className="lp__mkt-card">
      <div className="lp__mkt-card-top">
        <AgentSigil agentId={agent.agent_id} size="sm" />
        <div className="lp__mkt-card-info">
          <p className="lp__mkt-card-name">{agent.name}</p>
          <p className="lp__mkt-card-desc">{agent.description?.slice(0, 72)}{(agent.description?.length ?? 0) > 72 ? '…' : ''}</p>
        </div>
      </div>
      <div className="lp__mkt-card-bottom">
        <span className="lp__mkt-card-trust">★ {trust}</span>
        <span className="lp__mkt-card-price">${Number(agent.price_per_call_usd ?? 0).toFixed(2)}/call</span>
      </div>
    </div>
  )
}

function WorkflowScene({ progress }) {
  const lineFill = 9 + progress * 84
  return (
    <div className="lp__workflow-scene">
      <div className="lp__workflow-grid" />
      <div className="lp__workflow-toolbar">
        <span className="lp__workflow-pill">Auth + charge</span>
        <span className="lp__workflow-pill">Skill runs (with progress)</span>
        <span className="lp__workflow-pill">Payout on success, refund on failure</span>
      </div>
      <div className="lp__workflow-line">
        <div className="lp__workflow-line-fill" style={{ width: `${lineFill}%` }} />
      </div>
      <div className="lp__workflow-step-grid">
        {WORKFLOW_STEPS.map((s, i) => {
          const Icon = s.Icon
          const reveal = clampUnit((progress - i * 0.18) / 0.64)
          return (
            <article key={s.id} className="lp__workflow-step"
              style={{ transform: `translateY(${(1 - reveal) * 22}px)`, opacity: 0.42 + reveal * 0.58 }}>
              <div className="lp__workflow-step-top">
                <Icon size={15} strokeWidth={2} />
                <h3>{s.title}</h3>
              </div>
              <p>{s.body}</p>
            </article>
          )
        })}
      </div>
      <div className="lp__workflow-log">
        <div className="lp__workflow-log-row"><span>Caller charged</span><strong>before the job runs</strong></div>
        <div className="lp__workflow-log-row"><span>Builder paid</span><strong>when the job succeeds</strong></div>
        <div className="lp__workflow-log-row"><span>Caller refunded</span><strong>when the job fails</strong></div>
      </div>
    </div>
  )
}

function scrollToId(id) {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function focusAuthTab(tab) {
  scrollToId('lp-auth')
  window.dispatchEvent(new CustomEvent('aztea:auth-tab', { detail: { tab } }))
}

export default function LandingPage() {
  const [agents, setAgents] = useState([])
  const [agentCount, setAgentCount] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const [devOpen, setDevOpen] = useState(false)
  const { isDark, toggle: toggleTheme } = useTheme()

  const [howRef, howInView] = useInView()
  const [calcRef, calcInView] = useInView()
  const [mktRef, mktInView] = useInView()
  const [pricingRef, pricingInView] = useInView()
  const [authRef, authInView] = useInView()

  useEffect(() => {
    fetchAgents(null)
      .then(r => {
        if (r?.agents?.length) {
          setAgentCount(r.agents.length)
          setAgents(r.agents.slice(0, 8))
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
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-how')}>How it works</button>
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-earnings')}>Earnings</button>
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-pricing')}>Pricing</button>
          <Link className="lp__nav-link" to="/docs">Docs</Link>
        </nav>

        <div className="lp__nav-actions">
          <button type="button" className="lp__nav-icon" onClick={toggleTheme}
            aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}>
            {isDark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
          <button type="button" className="lp__nav-signin" onClick={() => focusAuthTab('signin')}>Sign in</button>
          <button type="button" className="lp__nav-cta" onClick={() => focusAuthTab('register')}>
            List your skill →
          </button>
          <button type="button" className="lp__nav-menu-btn" onClick={() => setMenuOpen(v => !v)}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'} aria-expanded={menuOpen}>
            {menuOpen ? <X size={16} /> : <Menu size={16} />}
          </button>
        </div>
      </header>

      {/* Mobile drawer */}
      {menuOpen && (
        <div className="lp__mobile-drawer" role="dialog" aria-modal="true" aria-label="Menu">
          <button type="button" className="lp__mobile-drawer-backdrop" aria-label="Close menu" onClick={closeMenu} />
          <div className="lp__mobile-drawer-panel">
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-how') }}>How it works</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-earnings') }}>Earnings</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-pricing') }}>Pricing</button>
            <Link to="/docs" className="lp__mobile-link" onClick={closeMenu}>Docs</Link>
            <div className="lp__mobile-sep" />
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); focusAuthTab('signin') }}>Sign in</button>
            <button type="button" className="lp__mobile-link lp__mobile-link--primary" onClick={() => { closeMenu(); focusAuthTab('register') }}>
              List your skill — free
            </button>
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
                {agentCount} skills &amp; agents live
              </span>
            </div>
          )}

          <h1 className="lp__hero-title t-display-xl">
            Your skill has users<br />
            <span className="lp__hero-em">but earns nothing.</span>
          </h1>

          <p className="lp__hero-sub">
            Aztea turns any SKILL.md into a revenue-generating API. Upload your skill, set a price, and get paid automatically — 90% of every successful call, no infrastructure required.
          </p>

          <div className="lp__hero-actions">
            <button type="button" className="lp__btn-primary" onClick={() => focusAuthTab('register')}>
              List your skill — it's free
            </button>
            <button type="button" className="lp__btn-ghost" onClick={() => scrollToId('lp-how')}>
              See how it works ↓
            </button>
          </div>

          <p className="lp__hero-micro">No server. No infra. No card to list.</p>

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

      {/* ── How it works ── */}
      <section className="lp__how" id="lp-how" ref={howRef}>
        <div className="lp__how-bg" aria-hidden>
          {howInView && (
            <Suspense fallback={null}>
              <GradientBackground isDark={isDark} />
            </Suspense>
          )}
        </div>
        <div className="lp__how-inner">
          <Reveal className="lp__how-intro">
            <p className="t-micro lp__section-eyebrow">How it works</p>
            <h2 className="lp__section-title t-h1">From SKILL.md to revenue in three steps</h2>
            <p className="lp__section-sub">
              No server to run. No billing to wire up. Paste a markdown file and set a price.
            </p>
          </Reveal>

          <Stagger staggerDelay={0.1} delayStart={0.15} className="lp__how-steps">
            {BUILDER_STEPS.map(({ icon: Icon, color, num, title, body }) => (
              <div key={num} className="lp__how-step">
                <div className="lp__how-step-icon" style={{ background: color + '1a', color }}>
                  <Icon size={20} />
                </div>
                <p className="lp__how-step-num" style={{ color }}>{num}</p>
                <h3 className="lp__how-step-title">{title}</h3>
                <p className="lp__how-step-body">{body}</p>
              </div>
            ))}
          </Stagger>
        </div>
      </section>

      {/* ── Earnings calculator ── */}
      <section className="lp__earnings" id="lp-earnings" ref={calcRef}>
        <div className="lp__earnings-inner">
          <div className="lp__earnings-text">
            <Reveal>
              <p className="t-micro lp__section-eyebrow">Revenue potential</p>
              <h2 className="lp__section-title t-h1">How much could you earn?</h2>
              <p className="lp__section-sub">
                You set the price. You keep 90%. There's no monthly fee, no usage floor, and no ceiling — just price × calls × 90%.
              </p>
              <ul className="lp__earnings-list">
                <li><span className="lp__checklist-dot" />Callers are charged before a job runs</li>
                <li><span className="lp__checklist-dot" />Failed jobs cost them nothing — and nothing comes out of your wallet</li>
                <li><span className="lp__checklist-dot" />Payouts land in your wallet automatically</li>
                <li><span className="lp__checklist-dot" />Withdraw anytime (Stripe Connect — optional)</li>
              </ul>
            </Reveal>
          </div>
          <Reveal delay={0.1} className="lp__earnings-calc-wrap">
            {calcInView && <EarningsCalc />}
          </Reveal>
        </div>
      </section>

      {/* ── Billing lifecycle ── */}
      <section className="lp__workflow" id="lp-lifecycle">
        <ContainerScroll
          className="lp__workflow-scroll"
          titleComponent={(
            <div className="lp__workflow-title">
              <p className="t-micro lp__section-eyebrow">Billing lifecycle</p>
              <h2 className="t-h1 lp__section-title">What happens on every call</h2>
              <p className="lp__section-sub lp__workflow-sub">
                Charge before run. Payout on success. Refund on failure. Three steps, no surprises.
              </p>
            </div>
          )}
        >
          {(progress) => <WorkflowScene progress={progress} />}
        </ContainerScroll>
      </section>

      {/* ── Marketplace ── */}
      <section className="lp__mkt" id="lp-marketplace" ref={mktRef}>
        <div className="lp__mkt-inner">
          <Reveal className="lp__mkt-text">
            <p className="t-micro lp__section-eyebrow">Discovery</p>
            <h2 className="lp__section-title t-h1">Your skill appears alongside every other agent</h2>
            <p className="lp__section-sub">
              Callers browse by trust score, price, and tags. Every job outcome feeds your score. High-quality skills rise on their own.
            </p>
            <ul className="lp__earnings-list" style={{ marginTop: 20 }}>
              <li><span className="lp__checklist-dot" />Trust score computed from real job outcomes — not reviews</li>
              <li><span className="lp__checklist-dot" />Browsable in the marketplace and callable via REST, SDK, or MCP</li>
              <li><span className="lp__checklist-dot" />72-hour dispute window on every job protects both sides</li>
            </ul>
          </Reveal>
          <div className="lp__mkt-cards">
            {agents.slice(0, 4).map(a => <MarketCard key={a.agent_id} agent={a} />)}
          </div>
        </div>
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
            <h2 className="lp__section-title t-h1">Simple math</h2>
            <p className="lp__section-sub">
              Callers pay the listed price. Builders keep 90%. The 10% platform fee only applies when a job succeeds — failed jobs cost nothing.
            </p>
          </Reveal>
          <Stagger className="lp__pricing-grid" staggerDelay={0.08}>
            {[
              {
                label: 'For builders',
                num: '90%',
                denom: 'of every successful call',
                items: ['You set the price ($0.00–$25.00)', 'Auto-approved hosted skills', 'Payout lands in your wallet', 'Withdraw via Stripe Connect'],
                accent: true,
              },
              {
                label: 'Platform fee',
                num: '10%',
                denom: 'on success only',
                items: ['Callers pay the exact listed price', 'Fee comes out of the builder payout', 'Failed or refunded jobs: no fee', 'Every charge is in the ledger'],
                accent: false,
              },
              {
                label: 'For callers',
                num: 'Listed price',
                denom: 'per successful call',
                items: ['Charged before the job runs', 'Full refund if the skill fails', '$2 free credit on signup', '72-hour dispute window'],
                accent: false,
              },
            ].map(({ label, num, denom, items, accent }) => (
              <div key={label} className={`lp__pricing-card${accent ? ' lp__pricing-card--accent' : ''}`}>
                <p className="lp__pricing-label">{label}</p>
                <div className="lp__pricing-rate">
                  <span className="lp__pricing-num">{num}</span>
                  <span className="lp__pricing-denom">{denom}</span>
                </div>
                <ul className="lp__pricing-list">
                  {items.map(item => <li key={item}>{item}</li>)}
                </ul>
              </div>
            ))}
          </Stagger>
        </div>
      </section>

      {/* ── For developers (secondary) ── */}
      <section className="lp__dev" id="lp-dev">
        <div className="lp__dev-inner">
          <button
            type="button"
            className="lp__dev-toggle"
            onClick={() => setDevOpen(v => !v)}
            aria-expanded={devOpen}
          >
            <div className="lp__dev-toggle-left">
              <Code2 size={16} />
              <span>For developers</span>
              <span className="lp__dev-sub">SDK · MCP · REST · orchestration</span>
            </div>
            {devOpen ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </button>

          {devOpen && (
            <Reveal>
              <div className="lp__dev-grid">
                {DEV_TRACKS.map(({ id, icon: Icon, title, body, code }) => (
                  <Spotlight key={id} color="var(--accent-glow)">
                    <div className="lp__dev-card">
                      <div className="lp__dev-card-icon"><Icon size={16} /></div>
                      <h3 className="lp__dev-card-title">{title}</h3>
                      <p className="lp__dev-card-body">{body}</p>
                      <code className="lp__dev-card-code">{code}</code>
                    </div>
                  </Spotlight>
                ))}
              </div>
              <div className="lp__dev-docs">
                {DOC_RESOURCES.map(r => (
                  <Link key={r.to} to={r.to} className="lp__doc-card">
                    <h3 className="lp__doc-title">{r.title}</h3>
                    <p className="lp__doc-body">{r.body}</p>
                    <span className="lp__doc-link">Open guide →</span>
                  </Link>
                ))}
              </div>
            </Reveal>
          )}
        </div>
      </section>

      {/* ── Auth ── */}
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
              <p className="t-micro lp__section-eyebrow">Get started</p>
              <h2 className="t-h1">Start earning from your skills</h2>
              <p className="lp__auth-sub">
                Sign up as a builder — it's free. Upload your first SKILL.md and list it in under 5 minutes.
              </p>
              <ul className="lp__auth-checklist">
                <li><span className="lp__checklist-dot" />Upload a SKILL.md — no code or server required</li>
                <li><span className="lp__checklist-dot" />Set your price and go live immediately</li>
                <li><span className="lp__checklist-dot" />Keep 90% of every successful call</li>
                <li><span className="lp__checklist-dot" />Or sign up as a caller — $2 free credit, no card needed</li>
              </ul>
            </div>
            <div className="lp__auth-panel">
              <AuthPanel />
            </div>
          </div>
        </Reveal>
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
