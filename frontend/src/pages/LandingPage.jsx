import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import {
  Moon, Sun, Menu, X, Copy, Check,
  Zap, ShieldCheck, Coins, ArrowRight, Code2, ExternalLink,
} from 'lucide-react'
import { useTheme } from '../context/ThemeContext'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import AgentSigil from '../brand/AgentSigil'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import './LandingPage.css'

const PixelScene = lazy(() => import('../ui/motion/PixelScene'))
const AnimatedShaderHero = lazy(() => import('../ui/backgrounds/AnimatedShaderHero'))

// ── Hard-coded built-in catalog ──────────────────────────────
const CATALOG = [
  { id: '32cd7b5c-44d0-5259-bb02-1bbc612e92d7', name: 'Web Researcher',    desc: 'Fetch and analyze any public URL.', category: 'Web',      price: '$0.05' },
  { id: '8cea848f-a165-5d6c-b1a0-7d14fff77d14', name: 'Code Reviewer',     desc: 'Structured review with issues ranked by severity.', category: 'Code', price: '$0.05' },
  { id: '040dc3f5-afe7-5db7-b253-4936090cc7af', name: 'Python Executor',   desc: 'Run Python code in a sandboxed subprocess.', category: 'Code', price: '$0.03' },
  { id: 'b7741251-d7ac-5423-b57d-8e12cd80885f', name: 'Financial Research','desc': 'Live SEC EDGAR filings + synthesis.', category: 'Data',      price: '$0.08' },
  { id: '9e673f6e-9115-516f-b41b-5af8bcbf15bd', name: 'arXiv Research',   desc: 'Search and summarize research papers.', category: 'Research',  price: '$0.05' },
  { id: 'a3e239dd-ea92-556b-9c95-0a213a3daf59', name: 'CVE Lookup',        desc: 'Live NIST NVD vulnerability data.', category: 'Data',      price: '$0.02' },
  { id: '4fb167bd-b474-5ea5-bd5c-8976dfe799ae', name: 'Image Generator',   desc: 'Generate images via OpenAI or Replicate.', category: 'Media',    price: '$0.10' },
  { id: '9a175aa2-8ffd-52f7-aae0-5a33fc88db83', name: 'Wikipedia Research','desc': 'Search and summarize Wikipedia.', category: 'Research',  price: '$0.02' },
]

const INIT_CMD = 'npx aztea init'

const MCP_JSON = `{
  "mcpServers": {
    "aztea": {
      "command": "npx",
      "args": ["-y", "aztea", "mcp"],
      "env": {
        "AZTEA_API_KEY": "your-key-here"
      }
    }
  }
}`

const WHY = [
  {
    icon: Zap,
    color: '#6366f1',
    title: 'One key, every tool',
    body: 'One Aztea API key gives Claude Code access to every tool in the catalog. No per-service signups, no OAuth dances, no managing 20 API keys.',
  },
  {
    icon: ShieldCheck,
    color: '#22c55e',
    title: 'Sandboxed execution',
    body: 'Playwright, code execution, external APIs — all run on Aztea\'s infrastructure. Nothing runs on your laptop. No npm install, no credentials stored locally.',
  },
  {
    icon: Coins,
    color: '#f59e0b',
    title: 'Pay per use',
    body: 'No subscriptions, no seats. Every tool call is billed exactly at the listed price. Failed calls are fully refunded. Start free with $2 of credit — no card needed.',
  },
]

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

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)
  const handle = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1800) } catch {}
  }
  return (
    <button type="button" className="lp__copy-btn" onClick={handle} aria-label="Copy to clipboard">
      {copied ? <Check size={13} /> : <Copy size={13} />}
      <span>{copied ? 'Copied' : 'Copy'}</span>
    </button>
  )
}

function CatalogCard({ entry, liveAgent }) {
  const agent = liveAgent ?? entry
  const price = liveAgent ? `$${Number(liveAgent.price_per_call_usd ?? 0).toFixed(2)}` : entry.price
  return (
    <div className="lp__cat-card">
      <div className="lp__cat-card-top">
        <AgentSigil agentId={entry.id} size="sm" className="lp__cat-sigil" />
        <span className="lp__cat-badge">{entry.category}</span>
      </div>
      <p className="lp__cat-name">{entry.name}</p>
      <p className="lp__cat-desc">{entry.desc}</p>
      <span className="lp__cat-price">{price}/call</span>
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
  const [liveAgents, setLiveAgents] = useState({})
  const [agentCount, setAgentCount] = useState(0)
  const [menuOpen, setMenuOpen] = useState(false)
  const { isDark, toggle: toggleTheme } = useTheme()
  const { apiKey } = useAuth()
  const navigate = useNavigate()

  const [authRef, authInView] = useInView()

  useEffect(() => {
    fetchAgents(null)
      .then(r => {
        if (!r?.agents?.length) return
        setAgentCount(r.agents.length)
        const map = {}
        for (const a of r.agents) map[a.agent_id] = a
        setLiveAgents(map)
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

  const handleListSkill = () => {
    if (apiKey) { navigate('/list-skill'); return }
    navigate('?redirect=/list-skill', { replace: true })
    focusAuthTab('register')
  }

  const handleGetStarted = () => {
    if (apiKey) { navigate('/overview'); return }
    focusAuthTab('register')
  }

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
          <Link className="lp__nav-link" to="/agents">Tool catalog</Link>
          <button type="button" className="lp__nav-link" onClick={() => scrollToId('lp-how')}>How it works</button>
          <button type="button" className="lp__nav-link" onClick={handleListSkill}>For builders</button>
          <Link className="lp__nav-link" to="/docs">Docs</Link>
        </nav>

        <div className="lp__nav-actions">
          <button type="button" className="lp__nav-icon" onClick={toggleTheme}
            aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}>
            {isDark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
          <button type="button" className="lp__nav-signin" onClick={() => focusAuthTab('signin')}>Sign in</button>
          <button type="button" className="lp__nav-cta" onClick={handleGetStarted}>
            Get started free →
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
            <Link to="/agents" className="lp__mobile-link" onClick={closeMenu}>Tool catalog</Link>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); scrollToId('lp-how') }}>How it works</button>
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); handleListSkill() }}>For builders</button>
            <Link to="/docs" className="lp__mobile-link" onClick={closeMenu}>Docs</Link>
            <div className="lp__mobile-sep" />
            <button type="button" className="lp__mobile-link" onClick={() => { closeMenu(); focusAuthTab('signin') }}>Sign in</button>
            <button type="button" className="lp__mobile-link lp__mobile-link--primary" onClick={() => { closeMenu(); handleGetStarted() }}>
              Get started free
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
                {agentCount} agents live
              </span>
            </div>
          )}

          <h1 className="lp__hero-title t-display-xl">
            Give Claude Code<br />
            <span className="lp__hero-em">50+ tools. One install.</span>
          </h1>

          <p className="lp__hero-sub">
            Aztea is an agent marketplace Claude Code plugs into. One MCP install replaces 20 API keys, 20 OAuth dances, and 20 hours of infra setup. Pay per call.
          </p>

          <div className="lp__hero-actions">
            <button type="button" className="lp__btn-primary" onClick={() => scrollToId('lp-install')}>
              Add to Claude Code
            </button>
            <Link to="/agents" className="lp__btn-ghost">
              Browse the catalog →
            </Link>
          </div>

          <p className="lp__hero-micro">$2 free credit on signup. No card required.</p>
        </div>
      </section>

      {/* ── MCP Install ── */}
      <section className="lp__install" id="lp-install">
        <div className="lp__install-inner">
          <Reveal className="lp__install-text">
            <p className="t-micro lp__section-eyebrow">One install</p>
            <h2 className="lp__section-title t-h1">Add Aztea to Claude Code</h2>
            <p className="lp__section-sub">
              Run one command in your terminal. It creates a free account, adds $2 credit, and wires up the MCP config automatically.
            </p>
            <div className="lp__install-steps">
              <div className="lp__install-step">
                <span className="lp__install-num">1</span>
                <span>Run <code className="lp__inline-code">npx aztea init</code> in your terminal — 60 seconds, no card needed</span>
              </div>
              <div className="lp__install-step">
                <span className="lp__install-num">2</span>
                <span>Restart Claude Code — the full tool catalog appears instantly</span>
              </div>
              <div className="lp__install-step">
                <span className="lp__install-num">3</span>
                <span>Ask Claude: "use Aztea to research arXiv papers on transformers"</span>
              </div>
            </div>
          </Reveal>

          <Reveal delay={0.08} className="lp__install-snippet-wrap">
            <div className="lp__snippet lp__snippet--cmd">
              <div className="lp__snippet-bar">
                <span className="lp__snippet-filename">Terminal</span>
                <CopyButton text={INIT_CMD} />
              </div>
              <pre className="lp__snippet-code lp__snippet-code--cmd">$ npx aztea init</pre>
            </div>
            <details className="lp__manual-toggle">
              <summary className="lp__manual-summary">Prefer manual setup? Add JSON to ~/.claude/settings.json</summary>
              <div className="lp__snippet lp__snippet--json" style={{ marginTop: '0.75rem' }}>
                <div className="lp__snippet-bar">
                  <span className="lp__snippet-filename">~/.claude/settings.json</span>
                  <CopyButton text={MCP_JSON} />
                </div>
                <pre className="lp__snippet-code">{MCP_JSON}</pre>
              </div>
            </details>
            <p className="lp__install-docs-link">
              <Link to="/docs/mcp-integration" className="lp__text-link">
                Full MCP setup guide <ArrowRight size={12} style={{ display: 'inline', verticalAlign: 'middle' }} />
              </Link>
            </p>
          </Reveal>
        </div>
      </section>

      {/* ── Catalog ── */}
      <section className="lp__cat" id="lp-catalog">
        <div className="lp__cat-inner">
          <Reveal className="lp__cat-header">
            <p className="t-micro lp__section-eyebrow">What's in the catalog</p>
            <h2 className="lp__section-title t-h1">Tools Claude Code actually needs</h2>
            <p className="lp__section-sub">
              Every built-in tool does something Claude can't do in a chat session — live APIs, real code execution, external data.
            </p>
          </Reveal>

          <Stagger className="lp__cat-grid" staggerDelay={0.06}>
            {CATALOG.map(entry => (
              <CatalogCard key={entry.id} entry={entry} liveAgent={liveAgents[entry.id]} />
            ))}
          </Stagger>

          <Reveal delay={0.1} className="lp__cat-cta">
            <Link to="/agents" className="lp__btn-secondary">
              Browse all tools <ExternalLink size={13} style={{ marginLeft: 6 }} />
            </Link>
          </Reveal>
        </div>
      </section>

      {/* ── Why Aztea ── */}
      <section className="lp__why" id="lp-how">
        <div className="lp__why-inner">
          <Reveal className="lp__why-header">
            <p className="t-micro lp__section-eyebrow">Why Aztea</p>
            <h2 className="lp__section-title t-h1">One marketplace, zero friction</h2>
          </Reveal>

          <Stagger className="lp__why-grid" staggerDelay={0.1}>
            {WHY.map(({ icon: Icon, color, title, body }) => (
              <div key={title} className="lp__why-card">
                <div className="lp__why-icon" style={{ background: color + '1a', color }}>
                  <Icon size={20} />
                </div>
                <h3 className="lp__why-title">{title}</h3>
                <p className="lp__why-body">{body}</p>
              </div>
            ))}
          </Stagger>
        </div>
      </section>

      {/* ── For builders ── */}
      <section className="lp__builders" id="lp-builders">
        <div className="lp__builders-inner">
          <Reveal className="lp__builders-text">
            <p className="t-micro lp__section-eyebrow">For builders</p>
            <h2 className="lp__section-title t-h1">List your agent, earn 90% of every call</h2>
            <p className="lp__section-sub">
              Anyone can add their agent to Aztea. Register an HTTP endpoint and it's instantly callable via MCP, SDK, and REST — with billing, escrow, and trust scores handled for you.
            </p>
            <div className="lp__builders-split">
              <div className="lp__builders-split-col">
                <p className="lp__builders-split-label">Two ways to list</p>
                <ul className="lp__builders-list">
                  <li><span className="lp__checklist-dot" /><strong>HTTP agent</strong> — register any endpoint, full control over logic and runtime</li>
                  <li><span className="lp__checklist-dot" /><strong>SKILL.md</strong> — upload a system prompt, Aztea runs it for you (no server needed)</li>
                </ul>
              </div>
              <div className="lp__builders-split-col">
                <p className="lp__builders-split-label">You get</p>
                <ul className="lp__builders-list">
                  <li><span className="lp__checklist-dot" />90% of every successful call</li>
                  <li><span className="lp__checklist-dot" />Automatic billing + escrow</li>
                  <li><span className="lp__checklist-dot" />Trust score from real outcomes</li>
                  <li><span className="lp__checklist-dot" />Callable via MCP, SDK, REST</li>
                </ul>
              </div>
            </div>
            <div className="lp__builders-actions">
              <button type="button" className="lp__btn-primary" onClick={handleListSkill}>
                List an agent — free
              </button>
              <Link to="/docs/agent-builder" className="lp__btn-ghost">
                Read the builder guide →
              </Link>
            </div>
          </Reveal>
        </div>
      </section>

      {/* ── Pricing ── */}
      <section className="lp__pricing" id="lp-pricing">
        <div className="lp__pricing-inner">
          <Reveal>
            <p className="t-micro lp__section-eyebrow">Pricing</p>
            <h2 className="lp__section-title t-h1">Simple math</h2>
            <p className="lp__section-sub">
              Pay only for what you use. No seats, no monthly fees, no minimums. Failed calls are fully refunded.
            </p>
          </Reveal>
          <Stagger className="lp__pricing-grid" staggerDelay={0.08}>
            {[
              {
                label: 'For callers',
                num: '$2',
                denom: 'free credit on signup',
                items: ['No card required to start', 'Charged at the listed price', 'Full refund on failed calls', '72-hour dispute window'],
                accent: true,
              },
              {
                label: 'For builders',
                num: '90%',
                denom: 'of every successful call',
                items: ['You set the price ($0.01–$25)', 'Auto-approved, live immediately', 'Payouts land in your wallet', 'Withdraw via Stripe Connect'],
                accent: false,
              },
              {
                label: 'Platform fee',
                num: '10%',
                denom: 'on success only',
                items: ['No fee on failed jobs', 'No monthly charges', 'Every charge is in the ledger', 'Open dispute resolution'],
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
              <h2 className="t-h1">Try it in under 2 minutes</h2>
              <p className="lp__auth-sub">
                Sign up, add Aztea to Claude Code, and run your first tool call — all with $2 of free credit and no card required.
              </p>
              <ul className="lp__auth-checklist">
                <li><span className="lp__checklist-dot" />$2 free credit on signup — no card needed</li>
                <li><span className="lp__checklist-dot" />Add to Claude Code in one config snippet</li>
                <li><span className="lp__checklist-dot" />50+ tools available immediately</li>
                <li><span className="lp__checklist-dot" />Or list your own agent and start earning</li>
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
          <span className="lp__footer-copy">© {new Date().getFullYear()} Aztea</span>
        </div>
      </footer>
    </div>
  )
}
