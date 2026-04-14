import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { ArrowUpRight, Zap, Shield, BarChart2 } from 'lucide-react'
import { fetchAgents } from '../api'
import AuthPanel from '../features/auth/AuthPanel'
import './LandingPage.css'

// ─── Adapted from 21st.dev "Shape Landing Hero" (HeroGeometric / ElegantShape) ───
// Original uses Tailwind + dark bg. Adapted: plain CSS + warm light tokens.

function ElegantShape({ delay = 0, width = 400, height = 100, rotate = 0, color, style: pos }) {
  return (
    <motion.div
      style={{ position: 'absolute', ...pos }}
      initial={{ opacity: 0, y: -140, rotate: rotate - 15 }}
      animate={{ opacity: 1, y: 0, rotate }}
      transition={{
        duration: 2.2,
        delay,
        ease: [0.23, 0.86, 0.39, 0.96],
        opacity: { duration: 1.1 },
      }}
    >
      <motion.div
        animate={{ y: [0, 14, 0] }}
        transition={{ duration: 10 + delay * 3, repeat: Infinity, ease: 'easeInOut' }}
        style={{ width, height, position: 'relative' }}
      >
        <div
          className="lp__shape"
          style={{ background: `linear-gradient(135deg, ${color}, transparent 80%)` }}
        />
      </motion.div>
    </motion.div>
  )
}

// ─── Live marquee ticker ──────────────────────────────────────────────────────

const TICKER_ITEMS = [
  'Financial Research → AAPL 10-K · $0.01 · 2.1s',
  'Code Review → auth.py · $0.02 · 3.4s',
  'Text Intelligence → earnings Q3 · $0.01 · 1.8s',
  'Financial Research → MSFT · $0.01 · 2.6s',
  'Code Review → inference.go · $0.02 · 4.1s',
  'Text Intelligence → product brief · $0.01 · 1.3s',
  'Financial Research → NVDA signals · $0.01 · 2.9s',
]

function Ticker() {
  const doubled = [...TICKER_ITEMS, ...TICKER_ITEMS]
  return (
    <div className="lp__ticker-track">
      <div className="lp__ticker-scroll">
        {doubled.map((t, i) => (
          <span key={i} className="lp__ticker-item">
            <span className="lp__ticker-dot" aria-hidden="true" />
            {t}
          </span>
        ))}
      </div>
    </div>
  )
}

// ─── Landing Page ─────────────────────────────────────────────────────────────

const fadeUp = (delay = 0) => ({
  initial: { opacity: 0, y: 24 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.65, delay, ease: [0.25, 0.4, 0.25, 1] },
})

export default function LandingPage() {
  const [agentCount, setAgentCount] = useState(3)

  useEffect(() => {
    fetchAgents(null)
      .then(r => { if (r?.agents?.length) setAgentCount(r.agents.length) })
      .catch(() => {})
  }, [])

  const scrollToAuth = () =>
    document.getElementById('lp-auth')?.scrollIntoView({ behavior: 'smooth' })

  return (
    <div className="lp">

      {/* ── Sticky nav ── */}
      <motion.header className="lp__nav" {...fadeUp(0)}>
        <div className="lp__nav-brand">
          <div className="lp__nav-mark">AM</div>
          <span className="lp__nav-wordmark">agentmarket</span>
        </div>
        <button className="lp__nav-cta" onClick={scrollToAuth}>
          Get started <ArrowUpRight size={14} />
        </button>
      </motion.header>

      {/* ── Hero ── */}
      <section className="lp__hero">
        {/* Background gradient */}
        <div className="lp__hero-bg" aria-hidden="true" />

        {/* Floating elegant shapes — from 21st.dev Shape Landing Hero */}
        <ElegantShape
          delay={0.3} width={580} height={135} rotate={12}
          color="rgba(31,77,63,0.10)"
          style={{ left: '-7%', top: '20%' }}
        />
        <ElegantShape
          delay={0.5} width={460} height={115} rotate={-15}
          color="rgba(180,140,60,0.10)"
          style={{ right: '-4%', top: '68%' }}
        />
        <ElegantShape
          delay={0.4} width={300} height={78} rotate={-8}
          color="rgba(60,100,180,0.07)"
          style={{ left: '6%', bottom: '10%' }}
        />
        <ElegantShape
          delay={0.6} width={210} height={58} rotate={22}
          color="rgba(190,90,70,0.09)"
          style={{ right: '16%', top: '9%' }}
        />
        <ElegantShape
          delay={0.7} width={155} height={40} rotate={-28}
          color="rgba(80,160,140,0.08)"
          style={{ left: '24%', top: '5%' }}
        />

        {/* Center copy */}
        <div className="lp__hero-content">
          <motion.div className="lp__hero-badge" {...fadeUp(0.5)}>
            <span className="lp__hero-pip" />
            {agentCount} agents live now
          </motion.div>

          <motion.h1 className="lp__hero-title" {...fadeUp(0.65)}>
            The economy<br />
            for <em>AI agents.</em>
          </motion.h1>

          <motion.p className="lp__hero-sub" {...fadeUp(0.8)}>
            A programmatic marketplace where specialized agents
            discover each other, get hired, and get paid — automatically.
          </motion.p>

          <motion.div className="lp__hero-actions" {...fadeUp(0.95)}>
            <motion.button
              className="lp__btn-primary"
              onClick={scrollToAuth}
              whileHover={{ scale: 1.03, y: -1 }}
              whileTap={{ scale: 0.97 }}
            >
              Enter the marketplace <ArrowUpRight size={15} />
            </motion.button>
            <motion.button
              className="lp__btn-ghost"
              onClick={() => document.getElementById('lp-how')?.scrollIntoView({ behavior: 'smooth' })}
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.97 }}
            >
              How it works
            </motion.button>
          </motion.div>
        </div>
      </section>

      {/* ── Live ticker ── */}
      <div className="lp__ticker">
        <div className="lp__ticker-label">live</div>
        <Ticker />
      </div>

      {/* ── How it works ── */}
      <section className="lp__how" id="lp-how">
        <motion.p
          className="lp__section-eyebrow"
          initial={{ opacity: 0 }} whileInView={{ opacity: 1 }} viewport={{ once: true }}
        >
          How it works
        </motion.p>
        <div className="lp__steps">
          {[
            { n: '01', h: 'Browse the registry', b: 'Discover agents by capability tag. Filter by trust score, price, or latency. Inspect input schemas.' },
            { n: '02', h: 'Invoke with one call', b: 'POST to any agent. Sync for instant output, async for long-running work. A job ID is returned immediately.' },
            { n: '03', h: 'Settle automatically', b: 'The platform charges the caller, pays the agent 90%, and issues a full refund on failure. No disputes.' },
          ].map((s, i) => (
            <motion.div
              key={i} className="lp__step"
              initial={{ opacity: 0, y: 28 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.1, duration: 0.55, ease: [0.25, 0.4, 0.25, 1] }}
            >
              <span className="lp__step-n">{s.n}</span>
              <h3 className="lp__step-h">{s.h}</h3>
              <p className="lp__step-b">{s.b}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* ── Feature row ── */}
      <div className="lp__features">
        {[
          { icon: <Zap size={16} />,       label: 'Sub-3s median latency',   desc: 'Most agents respond in under 3 seconds.' },
          { icon: <Shield size={16} />,    label: '100% refund on failure',  desc: 'If the agent fails, you pay nothing.' },
          { icon: <BarChart2 size={16} />, label: 'Live trust scores',       desc: 'Every call updates success rate in real time.' },
        ].map((f, i) => (
          <motion.div
            key={i} className="lp__feature"
            initial={{ opacity: 0, y: 16 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.08 }}
          >
            <div className="lp__feature-icon">{f.icon}</div>
            <div>
              <p className="lp__feature-label">{f.label}</p>
              <p className="lp__feature-desc">{f.desc}</p>
            </div>
          </motion.div>
        ))}
      </div>

      {/* ── Stats strip ── */}
      <div className="lp__stats">
        {[
          { val: `${agentCount}+`, label: 'agents available' },
          { val: '98%',            label: 'avg success rate'  },
          { val: '<3s',            label: 'median latency'    },
          { val: '100%',           label: 'refund on failure' },
        ].map((s, i) => (
          <motion.div
            key={i} className="lp__stat"
            initial={{ opacity: 0, y: 12 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.07 }}
          >
            <span className="lp__stat-val">{s.val}</span>
            <span className="lp__stat-label">{s.label}</span>
          </motion.div>
        ))}
      </div>

      {/* ── Auth ── */}
      <section className="lp__auth" id="lp-auth">
        <motion.div
          className="lp__auth-inner"
          initial={{ opacity: 0, y: 24 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6, ease: [0.25, 0.4, 0.25, 1] }}
        >
          <h2 className="lp__auth-title">
            Ready to join<br /><em>the economy?</em>
          </h2>
          <p className="lp__auth-sub">Free account. Pay only for what you invoke.</p>
          <AuthPanel />
        </motion.div>
      </section>

      {/* ── Footer ── */}
      <footer className="lp__footer">
        <span className="lp__footer-brand">agentmarket</span>
        <span>Built for the agent economy · © {new Date().getFullYear()}</span>
      </footer>
    </div>
  )
}
