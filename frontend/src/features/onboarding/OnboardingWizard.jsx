import { useState, useEffect, useRef, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import { ArrowRight, X, Wallet, Bot, Zap, ChevronLeft, Hammer, ListChecks, Coins } from 'lucide-react'
import { useAuth } from '../../context/AuthContext'
import { useMarket } from '../../context/MarketContext'
import './OnboardingWizard.css'

const STORAGE_KEY_PREFIX = 'aztea_onboarding_done'

// Inline visual widgets per step
function WalletVisual({ maxDollars = 1 }) {
  const [count, setCount] = useState(0)
  useEffect(() => {
    const start = Date.now()
    const duration = 1200
    const raf = requestAnimationFrame(function tick() {
      const p = Math.min((Date.now() - start) / duration, 1)
      const eased = 1 - Math.pow(1 - p, 3)
      setCount(eased)
      if (p < 1) requestAnimationFrame(tick)
    })
    return () => cancelAnimationFrame(raf)
  }, [])

  return (
    <div className="ob-visual ob-visual--wallet">
      <div className="ob-visual__card">
        <div className="ob-visual__card-label">Available balance</div>
        <div className="ob-visual__card-amount">
          ${(count * maxDollars).toFixed(2)}
        </div>
        <div className="ob-visual__card-badge">Free credit applied</div>
        <div className="ob-visual__card-row">
          <div className="ob-visual__tx">
            <div className="ob-visual__tx-dot ob-visual__tx-dot--green" />
            <span>Welcome bonus</span>
            <span className="ob-visual__tx-amt">+${maxDollars.toFixed(2)}</span>
          </div>
        </div>
      </div>
      <div className="ob-visual__glow ob-visual__glow--green" />
    </div>
  )
}

function SkillListVisual() {
  const skills = [
    { name: 'PDF Summariser', price: '$0.05/call', color: '#6366f1' },
    { name: 'SQL Explainer',  price: '$0.02/call', color: '#10b981' },
    { name: 'Code Reviewer',  price: '$0.08/call', color: '#f59e0b' },
  ]
  return (
    <div className="ob-visual ob-visual--agents">
      {skills.map((s, i) => (
        <motion.div
          key={s.name}
          className="ob-visual__agent-card"
          initial={{ opacity: 0, x: -12 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.12, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        >
          <div className="ob-visual__agent-icon" style={{ background: s.color + '22', color: s.color }}>
            <ListChecks size={14} />
          </div>
          <span className="ob-visual__agent-name">{s.name}</span>
          <span className="ob-visual__agent-score" style={{ color: s.color }}>{s.price}</span>
        </motion.div>
      ))}
      <div className="ob-visual__glow ob-visual__glow--violet" />
    </div>
  )
}

function EarningsVisual() {
  const [pct, setPct] = useState(0)
  useEffect(() => {
    const start = Date.now()
    const raf = requestAnimationFrame(function tick() {
      const p = Math.min((Date.now() - start) / 900, 1)
      setPct(1 - Math.pow(1 - p, 3))
      if (p < 1) requestAnimationFrame(tick)
    })
    return () => cancelAnimationFrame(raf)
  }, [])
  return (
    <div className="ob-visual ob-visual--wallet">
      <div className="ob-visual__card">
        <div className="ob-visual__card-label">Your earnings</div>
        <div className="ob-visual__card-amount">${(pct * 9.00).toFixed(2)}</div>
        <div className="ob-visual__card-badge">90% of every call</div>
        <div className="ob-visual__card-row">
          <div className="ob-visual__tx">
            <div className="ob-visual__tx-dot ob-visual__tx-dot--green" />
            <span>100 calls × $0.10</span>
            <span className="ob-visual__tx-amt">+$9.00</span>
          </div>
        </div>
      </div>
      <div className="ob-visual__glow ob-visual__glow--green" />
    </div>
  )
}

function AgentsVisual() {
  const agents = [
    { name: 'System Design Reviewer', color: '#6366f1', score: '9.6' },
    { name: 'Incident Response Commander', color: '#f59e0b', score: '9.5' },
    { name: 'Code Review Agent', color: '#10b981', score: '9.4' },
    { name: 'Scenario Simulator', color: '#ec4899', score: '9.3' },
  ]
  return (
    <div className="ob-visual ob-visual--agents">
      {agents.map((a, i) => (
        <motion.div
          key={a.name}
          className="ob-visual__agent-card"
          initial={{ opacity: 0, x: -12 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.1, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
        >
          <div className="ob-visual__agent-icon" style={{ background: a.color + '22', color: a.color }}>
            <Bot size={14} />
          </div>
          <span className="ob-visual__agent-name">{a.name}</span>
          <span className="ob-visual__agent-score" style={{ color: a.color }}>★ {a.score}</span>
        </motion.div>
      ))}
      <div className="ob-visual__glow ob-visual__glow--violet" />
    </div>
  )
}

function CallVisual() {
  const lines = [
    { text: '$ aztea call incident-response-commander \\', delay: 0 },
    { text: '  --incident "api latency spikes across regions"', delay: 0.15 },
    { text: '', delay: 0.3 },
    { text: '✓ Charged $0.01', color: '#10b981', delay: 0.45 },
    { text: '✓ Running...', color: '#10b981', delay: 0.65 },
    { text: '', delay: 0.8 },
    { text: 'Root cause candidates: cache saturation, DB pool pressure', color: '#a78bfa', delay: 0.9 },
    { text: 'Immediate actions: rate-limit + rollback + observability checks', color: '#a78bfa', delay: 1.0 },
  ]
  return (
    <div className="ob-visual ob-visual--call">
      <div className="ob-visual__terminal">
        <div className="ob-visual__terminal-bar">
          <span className="ob-visual__dot-r" />
          <span className="ob-visual__dot-y" />
          <span className="ob-visual__dot-g" />
          <span className="ob-visual__terminal-title">aztea</span>
        </div>
        <div className="ob-visual__terminal-body">
          {lines.map((l, i) => (
            <motion.div
              key={i}
              className="ob-visual__terminal-line"
              style={l.color ? { color: l.color } : {}}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: l.delay, duration: 0.3 }}
            >
              {l.text}
            </motion.div>
          ))}
        </div>
      </div>
      <div className="ob-visual__glow ob-visual__glow--amber" />
    </div>
  )
}

function makeHirerSteps(creditDollars) {
  return [
    {
      id: 'wallet',
      icon: Wallet,
      accentColor: '#22c55e',
      eyebrow: '01 / 03',
      title: `You start with\n$${creditDollars.toFixed(2)} free credit`,
      subtitle: 'No card needed',
      body: `We charge your wallet before an agent runs, and refund you in full if it fails. Your free credit covers about ${Math.round(creditDollars / 0.01)} calls at $0.01 each.`,
      cta: 'View my wallet',
      ctaPath: '/wallet',
      Visual: () => <WalletVisual maxDollars={creditDollars} />,
    },
    {
      id: 'agents',
      icon: Bot,
      accentColor: '#6366f1',
      eyebrow: '02 / 03',
      title: 'Pick an agent\nthat fits your task',
      subtitle: 'Every trust score is computed from real jobs',
      body: "The registry shows each agent's real success rate, price, and example outputs. Filter by what you need, sort by trust score, and run any agent directly in the browser.",
      cta: 'Browse agents',
      ctaPath: '/agents',
      Visual: AgentsVisual,
    },
    {
      id: 'call',
      icon: Zap,
      accentColor: '#f59e0b',
      eyebrow: '03 / 03',
      title: 'Use scoped keys\nbefore you automate',
      subtitle: 'One key per integration is the safe default',
      body: 'Create caller-only or worker-only keys in Settings. If a key leaks or needs rotating, only one integration is affected - not everything at once.',
      cta: 'Open settings',
      ctaPath: '/settings',
      Visual: CallVisual,
    },
  ]
}

const BUILDER_STEPS = [
  {
    id: 'list',
    icon: ListChecks,
    accentColor: '#6366f1',
    eyebrow: '01 / 03',
    title: 'Upload a SKILL.md\nand you\'re live',
    subtitle: 'No infrastructure required',
    body: 'Write a SKILL.md that describes what your skill does, set a price per call, and Aztea handles execution, billing, and delivery to callers.',
    cta: 'List a skill',
    ctaPath: '/list-skill',
    Visual: SkillListVisual,
  },
  {
    id: 'earn',
    icon: Coins,
    accentColor: '#22c55e',
    eyebrow: '02 / 03',
    title: 'You keep 90%\nof every call',
    subtitle: 'Aztea takes 10% as a platform fee',
    body: 'Every time a caller runs your skill, 90% of the price is credited to your wallet automatically. No invoicing, no delays.',
    cta: 'See how payment works',
    ctaPath: '/wallet',
    Visual: EarningsVisual,
  },
  {
    id: 'worker',
    icon: Hammer,
    accentColor: '#f59e0b',
    eyebrow: '03 / 03',
    title: 'Track jobs in\nyour worker dashboard',
    subtitle: 'Async jobs queue, you process them at your pace',
    body: 'The Worker tab shows every pending job for your skills. Claim, heartbeat, and complete them from the browser or via the SDK.',
    cta: 'Open worker',
    ctaPath: '/worker',
    Visual: CallVisual,
  },
]

export default function OnboardingWizard() {
  const { user } = useAuth()
  const { loading, jobs, wallet } = useMarket()
  const [visible, setVisible] = useState(false)
  const [step, setStep] = useState(0)
  const [dir, setDir] = useState(1)
  const dismissedRef = useRef(false)
  const navigate = useNavigate()
  const role = user?.role ?? 'both'
  const STEPS = role === 'builder'
    ? BUILDER_STEPS
    : makeHirerSteps(role === 'hirer' ? 2 : 1)
  const userId = String(user?.user_id || '').trim()
  const username = String(user?.username || '').trim()
  // Prefer user_id; fall back to username so we still persist even if user_id
  // races/absent. Empty only when truly unauthenticated.
  const storageKey = userId
    ? `${STORAGE_KEY_PREFIX}:${userId}`
    : (username ? `${STORAGE_KEY_PREFIX}:u:${username}` : '')
  const hasRecentActivity =
    (Array.isArray(jobs) && jobs.length > 0) ||
    Number(wallet?.balance_cents || 0) > 100

  useEffect(() => {
    // In-session dismiss wins over everything — prevents the wizard from
    // re-opening if storage writes fail, identity reshuffles, or activity
    // signals flicker after the user clicks Done.
    if (dismissedRef.current) {
      setVisible(false)
      return
    }
    if (!storageKey) {
      setVisible(false)
      return
    }
    // Dismiss as soon as we can prove the user is not new. Do NOT gate on
    // market `loading` - if the API stalls the wizard would never appear for
    // the exact users who need it most.
    if (!loading && hasRecentActivity) {
      try { localStorage.setItem(storageKey, '1') } catch {}
      setVisible(false)
      return
    }
    let stored = null
    try { stored = localStorage.getItem(storageKey) } catch {}
    setVisible(!stored)
  }, [storageKey, loading, hasRecentActivity])

  const dismiss = useCallback(() => {
    dismissedRef.current = true
    if (storageKey) {
      try { localStorage.setItem(storageKey, '1') } catch {}
    }
    setVisible(false)
  }, [storageKey])

  useEffect(() => {
    if (!visible) return undefined
    const onKeydown = (event) => {
      if (event.key === 'Escape') dismiss()
    }
    window.addEventListener('keydown', onKeydown)
    return () => window.removeEventListener('keydown', onKeydown)
  }, [visible, dismiss])

  const goTo = (next) => {
    setDir(next > step ? 1 : -1)
    setStep(next)
  }

  const handleCta = () => {
    const current = STEPS[step]
    if (step >= STEPS.length - 1) {
      dismiss()
      navigate(current.ctaPath)
    } else {
      navigate(current.ctaPath)
      goTo(step + 1)
    }
  }

  const handleNext = () => {
    if (step >= STEPS.length - 1) { dismiss(); return }
    goTo(step + 1)
  }

  if (!visible) return null

  const current = STEPS[step]
  const Icon = current.icon
  const { Visual } = current

  const contentVariants = {
    initial: { opacity: 0, x: dir * 32 },
    animate: { opacity: 1, x: 0 },
    exit:    { opacity: 0, x: dir * -32 },
  }

  return (
    <AnimatePresence>
      <motion.div
        className="ob-overlay"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        onClick={dismiss}
      >
        <motion.div
          className="ob-shell"
          onClick={e => e.stopPropagation()}
          initial={{ opacity: 0, scale: 0.96, y: 24 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          exit={{ opacity: 0, scale: 0.96, y: 24 }}
          transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
        >
          {/* Close */}
          <button className="ob-close" onClick={dismiss} aria-label="Close onboarding">
            <X size={15} />
          </button>

          {/* Left: Visual */}
          <div className="ob-left">
            <AnimatePresence mode="wait">
              <motion.div
                key={current.id + '-visual'}
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.95 }}
                transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
                style={{ width: '100%' }}
              >
                <Visual />
              </motion.div>
            </AnimatePresence>
          </div>

          {/* Right: Content */}
          <div className="ob-right">
            {/* Step indicator */}
            <div className="ob-steps">
              {STEPS.map((s, i) => (
                <button
                  key={s.id}
                  className={`ob-step-pip ${i === step ? 'active' : ''} ${i < step ? 'done' : ''}`}
                  onClick={() => goTo(i)}
                  style={i === step ? { background: current.accentColor } : {}}
                  aria-label={`Step ${i + 1}`}
                />
              ))}
            </div>

            <AnimatePresence mode="wait">
              <motion.div
                key={current.id}
                className="ob-content"
                variants={contentVariants}
                initial="initial"
                animate="animate"
                exit="exit"
                transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
              >
                <div
                  className="ob-icon"
                  style={{ background: current.accentColor + '1a', color: current.accentColor }}
                >
                  <Icon size={22} />
                </div>

                <p className="ob-eyebrow" style={{ color: current.accentColor }}>
                  {current.eyebrow}
                </p>

                <h2 className="ob-title">
                  {current.title.split('\n').map((line, i) => (
                    <span key={i}>{line}{i < current.title.split('\n').length - 1 && <br />}</span>
                  ))}
                </h2>

                <p className="ob-subtitle">{current.subtitle}</p>
                <p className="ob-body">{current.body}</p>
              </motion.div>
            </AnimatePresence>

            {/* Footer */}
            <div className="ob-footer">
              <div className="ob-footer-left">
                {step > 0 && (
                  <button className="ob-back" onClick={() => goTo(step - 1)}>
                    <ChevronLeft size={14} />
                    Back
                  </button>
                )}
              </div>
              <div className="ob-footer-right">
                <button className="ob-skip" onClick={handleNext}>
                  {step === STEPS.length - 1 ? 'Done' : 'Next'}
                </button>
                <button
                  className="ob-cta"
                  style={{ background: current.accentColor }}
                  onClick={handleCta}
                >
                  {current.cta}
                  <ArrowRight size={14} />
                </button>
              </div>
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
