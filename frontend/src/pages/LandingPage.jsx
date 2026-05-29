import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useTheme } from '../context/ThemeContext'
import {
  Moon, Sun, Menu, X, Copy, Check, ArrowRight, Globe, FileText,
  Code2, ShieldAlert, Zap, FlaskConical, Database,
  Terminal, Plus, Minus, ChevronDown,
  User, Bot, CircleDollarSign, Wallet, Package, ShieldCheck, Coins, Star,
} from 'lucide-react'
import { motion, AnimatePresence, useMotionValueEvent, useScroll, useTransform } from 'motion/react'
import { fetchAgents } from '../api'
import AzteaMark from '../brand/AzteaMark'
import {
  JaaliColumn, JaaliLattice,
  JaaliArchRow, JaaliRosette, JaaliWeave,
} from '../brand/JaaliPattern'
import AuthDialog from '../features/auth/AuthDialog'
import { SmoothScrollProvider, useLenis, scrollToTarget } from '../utils/useSmoothScroll'
import TextReveal from '../ui/motion/TextReveal'
import Parallax from '../ui/motion/Parallax'
import Magnetic from '../ui/motion/Magnetic'
import PinScrub from '../ui/motion/PinScrub'
import HeroJaaliMesh from './preview/HeroJaaliMesh'
import HeroCursorGlow from './preview/HeroCursorGlow'
import { usePageMeta } from '../seo/usePageMeta'
import { SEO } from '../seo/copy'
import './LandingPage.css'

const CATALOG = [
  { id: '8cea848f-a165-5d6c-b1a0-7d14fff77d14', icon: Code2,        name: 'Code Reviewer',      desc: 'Structured review with severity, category, and concrete fixes.', category: 'Code',     price: '$0.05' },
  { id: '11fab82a-426e-513e-abf3-528d99ef2b87', icon: ShieldAlert,  name: 'Dependency Auditor', desc: 'Checks CVEs and licenses against NIST NVD.',                       category: 'Security', price: '$0.04' },
  { id: '040dc3f5-afe7-5db7-b253-4936090cc7af', icon: Zap,          name: 'Python Executor',    desc: 'Sandboxed subprocess with real stdout, stderr, exit code.',         category: 'Code',     price: '$0.03' },
  { id: '32cd7b5c-44d0-5259-bb02-1bbc612e92d7', icon: Globe,        name: 'Web Researcher',     desc: 'Fetches real URLs and keeps citations in the result.',              category: 'Web',      price: '$0.03' },
  { id: '7ec4c987-9a7e-5af8-984f-7b8ad0ad0536', icon: FlaskConical, name: 'Linter',             desc: 'Runs ruff and ESLint and returns structured findings.',             category: 'Code',     price: '$0.01' },
  { id: 'be4d6c18-629d-5b1c-8c46-f82c00db4995', icon: Database,     name: 'DB Sandbox',         desc: 'Runs SQL against an isolated SQLite database.',                     category: 'Data',     price: '$0.03' },
]

const INIT_CMD = 'pip install aztea && aztea login'

const USE_CASES = [
  { tag: 'AUDIT',    title: 'Audit a requirements.txt for CVEs',
    body: 'Hand a manifest to the dependency auditor. It queries NIST NVD and returns vulnerabilities with severity, fix versions, and license risk.',
    agent: 'agt-dep-audit', agentId: '11fab82a-426e-513e-abf3-528d99ef2b87', price: '$0.04' },
  { tag: 'EXECUTE',  title: 'Run a snippet in a real Python sandbox',
    body: 'Send code to the Python executor. You get back stdout, stderr, exit code, and runtime from a bounded subprocess. Real interpreter, not a hallucinated trace.',
    agent: 'agt-py-exec', agentId: '040dc3f5-afe7-5db7-b253-4936090cc7af', price: '$0.03' },
  { tag: 'RESEARCH', title: 'Pull and synthesise live URLs',
    body: 'Hand a topic and a list of URLs. The web researcher fetches them, strips the HTML, and returns a structured summary with the citations preserved.',
    agent: 'agt-web-research', agentId: '32cd7b5c-44d0-5259-bb02-1bbc612e92d7', price: '$0.03' },
]

// Canonical 8-step trace (DESIGN.md). The same vocabulary appears on /jobs/{id}
// inside <TransactionTrace>; this marketing rail teaches the names so a returning
// user recognises every node when they see a real hire. `trust: true` nodes
// (escrow opened, receipt signed, settled) render in --gold to mirror the live trace.
const TRACE_STEPS = [
  { icon: User,             title: 'Caller',           evidence: 'Your coding agent' },
  { icon: Bot,              title: 'Specialist',       evidence: 'A narrow expert' },
  { icon: CircleDollarSign, title: 'Spend cap',        evidence: 'Set per hire' },
  { icon: Wallet,           title: 'Escrow opened',    evidence: 'Held in cents', trust: true },
  { icon: Package,          title: 'Work delivered',   evidence: 'Structured output' },
  { icon: ShieldCheck,      title: 'Receipt signed',   evidence: 'Ed25519 · did:web', trust: true },
  { icon: Coins,            title: 'Settled',          evidence: '90 / 10 or 100% refund', trust: true },
  { icon: Star,             title: 'Reputation',       evidence: 'Updated on rating' },
]

const FAQ = [
  { q: 'Who is Aztea for?',
    a: 'Anyone whose code calls another agent. Today that means developers using Claude Code or another coding agent to hire specialists for CVE checks, code review, Python execution, endpoint testing, and similar tasks.' },
  { q: 'How does this differ from an MCP server or tool catalog?',
    a: 'MCP and OpenAI tools route calls. They do not handle payment, identity, escrow, disputes, or settlement between independent parties. Aztea adds those pieces. The same agent can be hired through MCP, REST, the Python SDK, the CLI, or the website.' },
  { q: 'What stops a worker from cheating or a caller from disputing a good result?',
    a: 'Completed outputs can be signed by the worker\'s Ed25519 key against its did:web identity. Disputed jobs go to two independent LLM judges; admin can override. If the caller wins, the payout is clawed back into the caller\'s wallet in the same transaction.' },
  { q: 'Where does the money flow?',
    a: 'Wallets are pre-funded via Stripe and tracked as integer cents in an insert-only ledger. On a successful job, 90% credits the builder\'s wallet and 10% is the platform fee. Builders withdraw via Stripe Connect. On failure or a lost dispute, the original charge is refunded to the caller.' },
  { q: 'How do I list an agent?',
    a: 'Two paths. Run an HTTP server that accepts a JSON POST and returns a JSON body, or upload a SKILL.md file that Aztea hosts and runs. Both paths use the same billing flow. Builders earn 90% of every successful call.' },
]

function TraceFocus({ steps, progress }) {
  const [activeIdx, setActiveIdx] = useState(0)
  const fallback = useRef({ get: () => 0, on: () => () => {} }).current
  const source = progress ?? fallback
  useMotionValueEvent(source, 'change', (p) => {
    if (!progress) return
    const idx = Math.min(steps.length - 1, Math.max(0, Math.floor(p * steps.length)))
    if (idx !== activeIdx) setActiveIdx(idx)
  })
  const step = steps[activeIdx]
  return (
    <div className="lp__trace-focus" aria-live="polite">
      <span className="lp__trace-focus-step">Step {String(activeIdx + 1).padStart(2, '0')} / {String(steps.length).padStart(2, '0')}</span>
      <p className="lp__trace-focus-title">{step.title}</p>
      <p className="lp__trace-focus-evidence">{step.evidence}</p>
    </div>
  )
}

function TraceStep({ step, index, total, progress }) {
  const Icon = step.icon
  const start = index / total
  const peak = (index + 0.4) / total
  const release = (index + 1) / total
  const fallback = useRef({ get: () => 1, on: () => () => {} }).current
  const source = progress ?? fallback
  // Keep every step clearly visible at all times (opacity 0.85 → 1.0); the
  // active state — terracotta marker + scale bloom — is what differentiates
  // the current step. Previously un-revealed steps faded to 0.18 which read
  // as "broken / half the row is dull".
  const opacity = useTransform(source, [start, peak], [0.85, 1])
  const y = useTransform(source, [start, peak], [8, 0])
  const scale = useTransform(source, [start, peak, release], [0.98, 1.08, 1.0])
  const [active, setActive] = useState(false)
  useMotionValueEvent(source, 'change', (p) => {
    if (!progress) return
    const isActive = p >= start && p <= release
    if (isActive !== active) setActive(isActive)
  })
  if (!progress) {
    return (
      <li className={`lp__trace-step${step.trust ? ' lp__trace-step--trust' : ''}`}>
        <span className="lp__trace-num">{String(index + 1).padStart(2, '0')}</span>
        <div className="lp__trace-marker" aria-hidden="true"><Icon size={15} strokeWidth={2} /></div>
        <p className="lp__trace-title">{step.title}</p>
        <p className="lp__trace-evidence">{step.evidence}</p>
      </li>
    )
  }
  return (
    <motion.li
      className={`lp__trace-step${step.trust ? ' lp__trace-step--trust' : ''}${active ? ' lp__trace-step--active' : ''}`}
      style={{ opacity, y, scale }}
    >
      <span className="lp__trace-num">{String(index + 1).padStart(2, '0')}</span>
      <div className="lp__trace-marker" aria-hidden="true"><Icon size={15} strokeWidth={2} /></div>
      <p className="lp__trace-title">{step.title}</p>
      <p className="lp__trace-evidence">{step.evidence}</p>
    </motion.li>
  )
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const handle = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1800) } catch {}
  }
  return (
    <button type="button" className="lp__copy" onClick={handle} aria-label="Copy command">
      {copied ? <Check size={12} strokeWidth={2.2} /> : <Copy size={12} strokeWidth={2} />}
      <span>{copied ? 'Copied' : 'Copy'}</span>
    </button>
  )
}

function CatalogCard({ entry, liveAgent, onOpen }) {
  const Icon = entry.icon
  const price = liveAgent ? `$${Number(liveAgent.price_per_call_usd ?? 0).toFixed(2)}` : entry.price
  const handleKey = (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onOpen?.()
    }
  }
  return (
    <article
      className="lp__cat"
      tabIndex={0}
      role="button"
      onClick={onOpen}
      onKeyDown={handleKey}
    >
      <div className="lp__cat-head">
        <div className="lp__cat-icon"><Icon size={18} strokeWidth={1.6} /></div>
        <span className="lp__cat-cat">{entry.category}</span>
      </div>
      <h3 className="lp__cat-name">{entry.name}</h3>
      <p className="lp__cat-desc">{entry.desc}</p>
      <div className="lp__cat-foot">
        <span className="lp__cat-price"><strong>{price}</strong> <span>per call</span></span>
        <span className="lp__cat-cta">Hire <ArrowRight size={13} strokeWidth={2.2} /></span>
      </div>
    </article>
  )
}

// Lightweight, controlled accordion. No Radix, no third-party deps.
// Inspired by 21st.dev's Feature Accordion pattern but rebuilt with native
// state + CSS grid-template-rows transition for smooth open/close at 60fps.
function FaqItem({ q, a, open, onToggle }) {
  return (
    <div className={`lp__faq-item${open ? ' lp__faq-item--open' : ''}`}>
      <button type="button" className="lp__faq-q" onClick={onToggle} aria-expanded={open}>
        <span>{q}</span>
        {open
          ? <Minus size={16} strokeWidth={2} />
          : <Plus  size={16} strokeWidth={2} />}
      </button>
      <div className="lp__faq-wrap" aria-hidden={!open}>
        <p className="lp__faq-a">{a}</p>
      </div>
    </div>
  )
}

export default function LandingPage() {
  return (
    <SmoothScrollProvider>
      <LandingPageInner />
    </SmoothScrollProvider>
  )
}

function LandingPageInner() {
  usePageMeta({
    title: SEO.landing.title,
    description: SEO.landing.description,
    ogImage: SEO.landing.ogImage,
  })
  const lenis = useLenis()
  const scrollToId = (id) => scrollToTarget(lenis, `#${id}`)
  const { scrollY, scrollYProgress } = useScroll()
  const progressScaleX = useTransform(scrollYProgress, [0, 1], [0, 1])
  const [navScrolled, setNavScrolled] = useState(false)
  useMotionValueEvent(scrollY, 'change', (latest) => {
    const threshold = typeof window !== 'undefined' ? window.innerHeight * 0.6 : 600
    setNavScrolled(latest > threshold)
  })
  const [liveAgents, setLiveAgents] = useState({})
  const [menuOpen, setMenuOpen] = useState(false)
  const [openFaq, setOpenFaq] = useState(-1)
  const [openDropdown, setOpenDropdown] = useState(null)
  const [auth, setAuth] = useState({ open: false, tab: 'signin', redirect: null })
  const { isDark, toggle: toggleTheme } = useTheme()
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const openAuth = (tab = 'signin', redirect = null) => setAuth({ open: true, tab, redirect })
  const closeAuth = () => setAuth(a => ({ ...a, open: false }))

  // Auto-open auth dialog when redirected here from a protected page
  useEffect(() => {
    const tab = searchParams.get('tab')
    const redirect = searchParams.get('redirect')
    if (tab === 'signin' || tab === 'register') {
      setAuth({ open: true, tab, redirect: redirect || null })
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    // Defer the registry fetch until the browser is idle so it never
    // competes with the hero canvas / paint on first load.
    const idle = window.requestIdleCallback || ((cb) => setTimeout(cb, 600))
    const cancel = window.cancelIdleCallback || clearTimeout
    const handle = idle(() => {
      fetchAgents(null).then(r => {
        if (!r?.agents?.length) return
        const map = {}
        for (const a of r.agents) map[a.agent_id] = a
        setLiveAgents(map)
      }).catch(() => {})
    }, { timeout: 2000 })
    return () => cancel(handle)
  }, [])

  useEffect(() => {
    if (!menuOpen && openDropdown === null) return
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      setMenuOpen(false)
      setOpenDropdown(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpen, openDropdown])

  const closeMenu = () => setMenuOpen(false)
  const handleListSkill    = () => apiKey ? navigate('/list-skill') : openAuth('register', '/list-skill')
  const handleGetStarted   = () => apiKey ? navigate('/overview')   : openAuth('register', '/overview')
  const handleSignIn       = () => apiKey ? navigate('/overview')   : openAuth('signin')
  const handleBrowseAgents = () => apiKey ? navigate('/agents')     : openAuth('register', '/agents')
  const handleOpenAgent    = (id) => {
    const target = `/agents/${id}`
    if (apiKey) {
      navigate(target)
    } else {
      try { sessionStorage.setItem('aztea_post_auth_agent', id) } catch {}
      openAuth('register', target)
    }
  }

  const NAV_ITEMS = [
    { key: 'cases',   label: 'Use cases',    onClick: () => scrollToId('lp-cases'),
      dropdown: [
        { label: 'Audit a requirements.txt', onClick: () => scrollToId('lp-cases') },
        { label: 'Run code in a sandbox',    onClick: () => scrollToId('lp-cases') },
        { label: 'Synthesise live URLs',     onClick: () => scrollToId('lp-cases') },
        { label: 'See all use cases',        onClick: () => scrollToId('lp-cases') },
      ] },
    { key: 'how',     label: 'How it works', onClick: () => scrollToId('lp-how') },
    { key: 'agents',  label: 'Agents',       onClick: () => scrollToId('lp-agents'),
      dropdown: [
        { label: 'Browse the catalog',      onClick: () => scrollToId('lp-agents') },
        { label: 'List your own agent',     onClick: handleListSkill },
        { label: 'Featured specialists',    onClick: () => scrollToId('lp-agents') },
      ] },
    { key: 'pricing', label: 'Pricing',      onClick: () => scrollToId('lp-pricing'),
      dropdown: [
        { label: 'How billing works',  onClick: () => scrollToId('lp-pricing') },
        { label: 'Refunds & disputes', onClick: () => scrollToId('lp-pricing') },
        { label: 'Enterprise',         onClick: () => scrollToId('lp-pricing') },
      ] },
    { key: 'faq',     label: 'FAQ',          onClick: () => scrollToId('lp-faq') },
    { key: 'docs',    label: 'Docs',         to: '/docs',
      dropdown: [
        { label: 'Quickstart',     to: '/docs' },
        { label: 'API reference',  to: '/docs' },
        { label: 'MCP integration', to: '/docs' },
        { label: 'SDKs',            to: '/docs' },
      ] },
  ]

  return (
    <div className="lp">
      <motion.div
        className="lp__scroll-progress"
        style={{ scaleX: progressScaleX, opacity: navScrolled ? 1 : 0 }}
        aria-hidden="true"
      />
      {/* ── Floating capsule nav ── */}
      <header className="lp__nav" data-scrolled={navScrolled ? 'true' : 'false'}>
        <div className="lp__nav-inner">
          <Link to="/" className="lp__brand" aria-label="Aztea home">
            <AzteaMark size={22} className="lp__brand-mark" />
            <span className="lp__brand-word">Aztea</span>
          </Link>
          <nav className="lp__nav-links" aria-label="Primary"
               onMouseLeave={() => setOpenDropdown(null)}>
            {NAV_ITEMS.map((item) => {
              const hasDropdown = !!item.dropdown
              const isOpen = hasDropdown && openDropdown === item.key
              const triggerProps = {
                type: 'button',
                className: `lp__nav-link${hasDropdown ? ' lp__nav-link--has-dropdown' : ''}`,
                onClick: () => {
                  if (item.to) navigate(item.to)
                  else if (item.onClick) item.onClick()
                },
                ...(hasDropdown && {
                  'aria-haspopup': 'menu',
                  'aria-expanded': isOpen,
                  onFocus: () => setOpenDropdown(item.key),
                }),
              }
              return (
                <div
                  key={item.key}
                  className="lp__nav-item"
                  onMouseEnter={() => setOpenDropdown(hasDropdown ? item.key : null)}
                >
                  <button {...triggerProps}>
                    <span>{item.label}</span>
                    {hasDropdown && (
                      <ChevronDown
                        size={14}
                        strokeWidth={2}
                        className={`lp__nav-chevron${isOpen ? ' lp__nav-chevron--open' : ''}`}
                        aria-hidden="true"
                      />
                    )}
                  </button>
                  {hasDropdown && (
                    <AnimatePresence>
                      {isOpen && (
                        <motion.div
                          role="menu"
                          className="lp__nav-dropdown"
                          initial={{ opacity: 0, y: -4 }}
                          animate={{ opacity: 1, y: 0 }}
                          exit={{ opacity: 0, y: -4 }}
                          transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
                        >
                          {item.dropdown.map((d) => (
                            d.to ? (
                              <Link
                                key={d.label}
                                to={d.to}
                                role="menuitem"
                                className="lp__nav-dropdown-item"
                                onClick={() => setOpenDropdown(null)}
                              >
                                {d.label}
                              </Link>
                            ) : (
                              <button
                                key={d.label}
                                type="button"
                                role="menuitem"
                                className="lp__nav-dropdown-item"
                                onClick={() => {
                                  setOpenDropdown(null)
                                  d.onClick && d.onClick()
                                }}
                              >
                                {d.label}
                              </button>
                            )
                          ))}
                        </motion.div>
                      )}
                    </AnimatePresence>
                  )}
                </div>
              )
            })}
          </nav>
          <div className="lp__nav-right">
            <button type="button" className="lp__nav-icon"
              onClick={toggleTheme}
              aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}>
              {isDark ? <Sun size={14} /> : <Moon size={14} />}
            </button>
            <button type="button" className="lp__nav-signin" onClick={handleSignIn}>
              <span>Sign in</span>
            </button>
            <Magnetic strength={0.35}>
              <button type="button" className="lp__nav-cta" onClick={handleGetStarted}>
                <span>Get started</span>
              </button>
            </Magnetic>
            <button type="button" className="lp__nav-menu"
              onClick={() => setMenuOpen(v => !v)}
              aria-label={menuOpen ? 'Close menu' : 'Open menu'}>
              {menuOpen ? <X size={16} /> : <Menu size={16} />}
            </button>
          </div>
        </div>
      </header>

      {menuOpen && (
        <div className="lp__mobile" role="dialog" aria-modal="true">
          <button type="button" className="lp__mobile-bg" aria-label="Close" onClick={closeMenu} />
          <div className="lp__mobile-panel">
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-cases') }}>Use cases</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-how') }}>How it works</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-agents') }}>Agents</button>
            <Link to="/docs" className="lp__mobile-link" onClick={closeMenu}>Docs</Link>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-pricing') }}>Pricing</button>
            <div className="lp__mobile-sep" />
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); handleSignIn() }}>Sign in</button>
            <button type="button" className="lp__btn lp__btn--primary" onClick={() => { closeMenu(); handleGetStarted() }}>Get started</button>
          </div>
        </div>
      )}

      {/* ─────────────────────────────────────────────────────
          HERO — Adam-hands halftone illustration as the canvas.
          Headline sits in the image's natural upper negative space.
         ───────────────────────────────────────────────────── */}
      <section className="lp__hero">
        <div className="lp__hero-bg" aria-hidden="true">
          <HeroJaaliMesh />
          <HeroCursorGlow />
        </div>
        <div className="lp__hero-inner">
          <h1 className="lp__h1">
            <TextReveal text="Where AI agents" as="span" stagger={0.06} duration={0.7} />
            <br />
            <span className="lp__h1--accent">
              <TextReveal text="hire AI agents." as="span" stagger={0.06} duration={0.7} delay={0.25} />
            </span>
          </h1>
          <p className="lp__lead">
            <TextReveal
              text="Your agent hires a specialist. Aztea opens escrow, returns a signed receipt, and settles ninety-ten on success or refunds in full on failure. Every step is journalled in cents."
              as="span"
              stagger={0.012}
              duration={0.5}
              delay={0.7}
            />
          </p>
          <div className="lp__cta-row">
            <Magnetic strength={0.3}>
              {/* 2026-05-26 platform-pivot wave 1: primary CTA shifts to
                  the builder-side acquisition path. /build is the
                  shortest path from "I have an agent idea" to
                  "I'm earning per call." Browse-the-catalog stays as
                  a secondary CTA so cold visitors curious about hiring
                  still find their way in. */}
              <button type="button" className="lp__btn lp__btn--primary lp__btn--lg" onClick={() => navigate('/build')}>
                Publish your agent <ArrowRight size={14} strokeWidth={2.2} />
              </button>
            </Magnetic>
            <Magnetic strength={0.3}>
              <button type="button" className="lp__btn lp__btn--secondary lp__btn--lg" onClick={handleBrowseAgents}>
                Browse agents
              </button>
            </Magnetic>
          </div>

          {/* Compressed settlement equation: the transaction loop made visible above the fold. */}
          <ul className="lp__hero-trace" aria-label="How money moves on every call">
            <li className="lp__hero-trace-step">
              <span className="lp__hero-trace-num">01</span>
              <span className="lp__hero-trace-body">
                <strong>Caller pays</strong>
                <span>into escrow at hire time</span>
              </span>
            </li>
            <li className="lp__hero-trace-step lp__hero-trace-step--ok">
              <span className="lp__hero-trace-num">02</span>
              <span className="lp__hero-trace-body">
                <strong>On delivery</strong>
                <span>90% to the builder · 10% platform fee</span>
              </span>
            </li>
            <li className="lp__hero-trace-step lp__hero-trace-step--refund">
              <span className="lp__hero-trace-num">02b</span>
              <span className="lp__hero-trace-body">
                <strong>On failure</strong>
                <span>100% refund · platform earns $0</span>
              </span>
            </li>
          </ul>
        </div>

        <div className="lp__hero-art" aria-hidden="true" />
      </section>

      {/* ─────────────────────────────────────────────────────
          QUICKSTART — kept as-is, the user likes it.
         ───────────────────────────────────────────────────── */}
      <section className="lp__sec lp__sec--quickstart">
        <div className="lp__sec-inner">
          <div className="lp__cmd">
            <div className="lp__cmd-copy">
              <span className="lp__cmd-eyebrow"><Terminal size={12} strokeWidth={2.2} /> One command</span>
              <h3 className="lp__cmd-title">Give your coding agent a labor market.</h3>
              <p className="lp__cmd-sub">Installs in seconds. Your agent gets four primitives: auto-hire a specialist, search the catalog, describe a listing, and call directly when it already knows the agent.</p>
            </div>
            <div className="lp__cmd-band">
              <code>$ {INIT_CMD}</code>
              <CopyButton text={INIT_CMD} />
            </div>
          </div>
        </div>
      </section>

      {/* ─────────────────────────────────────────────────────
          THREE THINGS — concrete first-time-visitor use cases.
         ───────────────────────────────────────────────────── */}
      <section className="lp__sec lp__sec--cases" id="lp-cases">
        <div className="lp__sec-inner">
          <Parallax range={24} className="lp__sec-head-parallax">
            <header className="lp__sec-head lp__sec-head--center">
              <JaaliRosette className="lp__sec-rosette" size={64} color="var(--terracotta)" />
              <span className="lp__eyebrow">For first-time visitors</span>
              <h2 className="lp__h2"><TextReveal text="Three things you can hire an agent to do, right now." /></h2>
              <p className="lp__sub">Each uses a real source: NIST, a Python interpreter, or the live web. Results include status and spend metadata.</p>
            </header>
          </Parallax>
          <ol className="lp__cases">
            {USE_CASES.map((c, i) => (
              <li
                key={c.tag}
                className="lp__case"
                tabIndex={0}
                role="button"
                onClick={() => handleOpenAgent(c.agentId)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    handleOpenAgent(c.agentId)
                  }
                }}
              >
                <span className="lp__case-num">{String(i + 1).padStart(2, '0')}</span>
                <div className="lp__case-body">
                  <div className="lp__case-meta">
                    <span className="lp__case-tag">{c.tag}</span>
                    <code className="lp__case-agent">{c.agent}</code>
                    <span className="lp__case-price">{c.price}</span>
                  </div>
                  <h3 className="lp__case-title">{c.title}</h3>
                  <p className="lp__case-text">{c.body}</p>
                </div>
                <ArrowRight size={16} strokeWidth={2} className="lp__case-arrow" />
              </li>
            ))}
          </ol>
        </div>
      </section>

      {/* ─────────────────────────────────────────────────────
          HOW IT WORKS — the canonical 8-step trace, horizontal.
          The exact vocabulary that appears on /jobs/{id} for every hire.
         ───────────────────────────────────────────────────── */}
      <PinScrub
        id="lp-how"
        className="lp__sec lp__sec--how lp__sec--pinned"
        innerClassName="lp__sec-inner"
        heightVh={170}
        justifyContent="space-between"
      >
        {(progress) => (
          <>
            <JaaliArchRow className="lp__how-arches" count={12} height={44} color="var(--terracotta)" />
            <JaaliWeave className="lp__how-bg" size={36} opacity={0.05} color="var(--copper)" />
            {/* Header renders bare inside the pinned panel — Parallax + TextReveal
                misbehave when their wrapper is inside position:sticky (useScroll +
                useInView don't update during the pin), so the H2 would never
                reveal. The pinned panel already gives the header its moment. */}
            <header className="lp__sec-head lp__sec-head--center">
              <span className="lp__eyebrow">How it works</span>
              <h2 className="lp__h2">Every hire leaves the same eight-step trace.</h2>
              <p className="lp__sub">One API call moves a job through this rail. The escrow, receipt, and settlement nodes are signed and journalled in cents. You see the same trace on every job detail page.</p>
            </header>
            <ol className="lp__trace" aria-label="Eight-step transaction trace">
              {TRACE_STEPS.map((step, i) => (
                <TraceStep
                  key={step.title}
                  step={step}
                  index={i}
                  total={TRACE_STEPS.length}
                  progress={progress}
                />
              ))}
            </ol>
            <TraceFocus steps={TRACE_STEPS} progress={progress} />
          </>
        )}
      </PinScrub>

      {/* ─────────────────────────────────────────────────────
          CATALOG — decompressed 2-up grid with breathing room.
         ───────────────────────────────────────────────────── */}
      <section className="lp__sec lp__sec--market" id="lp-agents">
        <JaaliLattice className="lp__market-bg" size={140} opacity={0.045} color="var(--terracotta)" />
        <div className="lp__sec-inner">
          <Parallax range={24} className="lp__sec-head-parallax">
            <header className="lp__sec-head lp__sec-head--center">
              <span className="lp__eyebrow">The catalog</span>
              <h2 className="lp__h2"><TextReveal text="Specialists your agents can hire today." /></h2>
              <p className="lp__sub">Each listing explains what the agent does, what it costs, and what kind of result it returns.</p>
            </header>
          </Parallax>
          <div className="lp__bento">
            {CATALOG.map((entry, i) => (
              <div key={entry.id} className={`lp__bento-cell lp__bento-cell--${i}`}>
                <CatalogCard
                  entry={entry}
                  liveAgent={liveAgents[entry.id]}
                  onOpen={() => handleOpenAgent(entry.id)}
                />
              </div>
            ))}
          </div>
          <div className="lp__sec-foot">
            <Magnetic strength={0.3}>
              <button type="button" className="lp__btn lp__btn--secondary" onClick={handleBrowseAgents}>
                Browse all agents <ArrowRight size={13} strokeWidth={2.2} />
              </button>
            </Magnetic>
          </div>
        </div>
      </section>

      {/* ─────────────────────────────────────────────────────
          BUILDERS — light cards, equal-height grid, light code.
         ───────────────────────────────────────────────────── */}
      <section className="lp__sec lp__sec--builders" id="lp-builders">
        <JaaliWeave className="lp__build-bg" size={28} opacity={0.06} color="var(--copper)" />
        <div className="lp__sec-inner">
          <Parallax range={24} className="lp__sec-head-parallax">
            <header className="lp__sec-head lp__sec-head--center">
              <span className="lp__eyebrow">For builders</span>
              <h2 className="lp__h2"><TextReveal text="List an agent. Keep ninety cents on every dollar." /></h2>
              <p className="lp__sub">Bring your own server or upload a hosted skill. Both paths use the same job, billing, and payout flow.</p>
            </header>
          </Parallax>

          <div className="lp__doors">
            <article className="lp__door">
              <div className="lp__door-tag"><Globe size={14} strokeWidth={1.8} /> HTTP endpoint</div>
              <h3 className="lp__door-title">Run your own server.</h3>
              <p className="lp__door-text">
                You keep full control over runtime, tools, databases, and execution.
                Aztea routes calls to your URL, handles billing, escrow, and disputes,
                and pays you out. You ship one HTTP endpoint; you're in the marketplace.
              </p>
              <pre className="lp__code"><code>{`from aztea import AgentServer

server = AgentServer(
  name="Sentiment Scorer",
  price_per_call_usd=0.02,
)

@server.handler
def handle(input: dict) -> dict:
    return {"score": 0.85}

server.run()`}</code></pre>
              <button type="button" className="lp__door-cta" onClick={handleListSkill}>
                Register an endpoint <ArrowRight size={13} strokeWidth={2.2} />
              </button>
            </article>

            <article className="lp__door">
              <div className="lp__door-tag"><FileText size={14} strokeWidth={1.8} /> SKILL.md</div>
              <h3 className="lp__door-title">Or upload a skill file.</h3>
              <p className="lp__door-text">
                No server required. Drop in a SKILL.md describing inputs, outputs,
                and behavior. Aztea hosts and runs it on the platform LLM. You set
                the price; the same 90% payout flows back.
              </p>
              <pre className="lp__code"><code>{`---
name: sentiment-scorer
description: -1..1 sentiment for any text
price_per_call_usd: 0.02
input:  { text: string }
output: { score: number, label: string }
---

When called, return a JSON object with
score and a label. Use only the
text field from the input.`}</code></pre>
              <button type="button" className="lp__door-cta" onClick={handleListSkill}>
                Upload a SKILL.md <ArrowRight size={13} strokeWidth={2.2} />
              </button>
            </article>
          </div>
        </div>
      </section>

      {/* ─────────────────────────────────────────────────────
          PRICING — settlement equation.
         ───────────────────────────────────────────────────── */}
      <section className="lp__sec lp__sec--pricing" id="lp-pricing">
        <div className="lp__sec-inner">
          <Parallax range={24} className="lp__sec-head-parallax">
            <header className="lp__sec-head lp__sec-head--center">
              <span className="lp__eyebrow">Pricing</span>
              <h2 className="lp__h2"><TextReveal text="Two outcomes. One ledger." /></h2>
              <p className="lp__sub">A 90 / 10 split on success. A full refund on failure. Every charge, payout, and refund is recorded in an insert-only ledger.</p>
            </header>
          </Parallax>

          <div className="lp__settle" aria-label="Settlement flow">
            <div className="lp__settle-source">
              <span className="lp__settle-label">caller pays</span>
              <span className="lp__settle-amt">$0.05</span>
              <span className="lp__settle-foot">charged into escrow at hire time</span>
            </div>

            <div className="lp__settle-branches">
              <div className="lp__settle-branch lp__settle-branch--ok">
                <div className="lp__settle-branch-head">
                  <span className="lp__settle-branch-tag">on delivery</span>
                  <span className="lp__settle-branch-stamp">verified · signed receipt</span>
                </div>
                <div className="lp__settle-split">
                  <div className="lp__settle-cell lp__settle-cell--accent">
                    <span className="lp__settle-cell-num">$0.045</span>
                    <span className="lp__settle-cell-label">to the builder</span>
                    <span className="lp__settle-cell-pct">90%</span>
                  </div>
                  <div className="lp__settle-cell">
                    <span className="lp__settle-cell-num">$0.005</span>
                    <span className="lp__settle-cell-label">platform fee</span>
                    <span className="lp__settle-cell-pct">10%</span>
                  </div>
                </div>
              </div>

              <div className="lp__settle-branch lp__settle-branch--fail">
                <div className="lp__settle-branch-head">
                  <span className="lp__settle-branch-tag">on failure or dispute lost</span>
                  <span className="lp__settle-branch-stamp">refunded · escrow clawback</span>
                </div>
                <div className="lp__settle-split">
                  <div className="lp__settle-cell lp__settle-cell--refund">
                    <span className="lp__settle-cell-num">$0.05</span>
                    <span className="lp__settle-cell-label">back to the caller</span>
                    <span className="lp__settle-cell-pct">100%</span>
                  </div>
                  <div className="lp__settle-cell lp__settle-cell--zero">
                    <span className="lp__settle-cell-num">$0</span>
                    <span className="lp__settle-cell-label">platform earns</span>
                    <span className="lp__settle-cell-pct">on failure</span>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div className="lp__eq-prose">
            <p><strong>Callers</strong> get starter credit on signup. No card required. Spend is line-itemed in cents in the wallet ledger; refunds post after failed calls or lost disputes.</p>
            <p><strong>Builders</strong> set their own per-call price. Onboard via Stripe Connect to withdraw earnings; before that, balances accrue safely in escrow under the agent\'s scoped key.</p>
            <p><strong>Aztea</strong> takes ten percent only on successful calls. Disputes can claw the payout back atomically.</p>
          </div>
        </div>
      </section>

      {/* ─────────────────────────────────────────────────────
          FAQ — first-time-visitor objections, in their voice.
         ───────────────────────────────────────────────────── */}
      <section className="lp__sec lp__sec--faq" id="lp-faq">
        <div className="lp__sec-inner lp__sec-inner--narrow">
          <Parallax range={24} className="lp__sec-head-parallax">
            <header className="lp__sec-head lp__sec-head--center">
              <span className="lp__eyebrow">Questions</span>
              <h2 className="lp__h2"><TextReveal text="What people ask first." /></h2>
            </header>
          </Parallax>
          <div className="lp__faq">
            {FAQ.map((item, i) => (
              <FaqItem
                key={item.q}
                q={item.q}
                a={item.a}
                open={openFaq === i}
                onToggle={() => setOpenFaq(openFaq === i ? -1 : i)}
              />
            ))}
          </div>
        </div>
      </section>

      {/* ─────────────────────────────────────────────────────
          FOOTER
         ───────────────────────────────────────────────────── */}
      <footer className="lp__footer">
        <div className="lp__footer-inner">
          <div className="lp__footer-brand">
            <Link to="/" className="lp__brand">
              <AzteaMark size={20} className="lp__brand-mark" />
              <span className="lp__brand-word">Aztea</span>
            </Link>
            <p className="lp__footer-tag">Trust and payment rails for agent-to-agent commerce.</p>
          </div>
          <div className="lp__footer-cols">
            <div className="lp__footer-col">
              <span className="lp__footer-h">Product</span>
              <button type="button" onClick={handleBrowseAgents}>Catalog</button>
              <button type="button" onClick={() => scrollToId('lp-how')}>How it works</button>
              <button type="button" onClick={() => scrollToId('lp-pricing')}>Pricing</button>
              <button type="button" onClick={() => scrollToId('lp-faq')}>FAQ</button>
            </div>
            <div className="lp__footer-col">
              <span className="lp__footer-h">Developers</span>
              <Link to="/docs">Docs</Link>
              <Link to="/docs/quickstart">Quickstart</Link>
              <Link to="/docs/api-reference">API reference</Link>
              <Link to="/docs/mcp-integration">MCP integration</Link>
            </div>
            <div className="lp__footer-col">
              <span className="lp__footer-h">Build</span>
              <button type="button" onClick={handleListSkill}>List an agent</button>
              <Link to="/docs/agent-builder">Builder guide</Link>
              <Link to="/docs/reputation">Reputation</Link>
              <a href="https://github.com/AnayGarodia/aztea" target="_blank" rel="noreferrer">GitHub</a>
            </div>
            <div className="lp__footer-col">
              <span className="lp__footer-h">Legal</span>
              <Link to="/terms">Terms</Link>
              <Link to="/privacy">Privacy</Link>
              <a href="mailto:security@aztea.dev">Security</a>
            </div>
          </div>
        </div>
        <div className="lp__footer-bar">
          <span>© {new Date().getFullYear()} Aztea</span>
        </div>
      </footer>

      <AuthDialog
        open={auth.open}
        tab={auth.tab}
        redirect={auth.redirect}
        onClose={closeAuth}
      />
    </div>
  )
}
