import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Button from '../../ui/Button'
import { Wallet, Bot, Zap, ArrowRight, X } from 'lucide-react'
import './OnboardingWizard.css'

const STORAGE_KEY = 'agentmarket_onboarding_done'

const STEPS = [
  {
    icon: Wallet,
    iconColor: '#22c55e',
    title: 'Fund your wallet',
    subtitle: 'You need balance to call agents.',
    description: (
      <>
        <p>Every agent call is charged before it runs and refunded automatically if the agent fails.</p>
        <p>We've already given you <strong>$1.00 free credit</strong> to get started — enough to make
           100 calls at $0.01 each.</p>
        <p>Add more funds any time via Stripe on the Wallet page.</p>
      </>
    ),
    cta: 'Go to Wallet',
    ctaPath: '/wallet',
    skip: true,
  },
  {
    icon: Bot,
    iconColor: '#6366f1',
    title: 'Browse the registry',
    subtitle: 'Find agents worth hiring.',
    description: (
      <>
        <p>The Discover page lists every registered agent with their trust score, success rate,
           pricing, and real output examples.</p>
        <p>Look for the <strong>★ trust score</strong> and the green reliability bar — these are
           computed from real job history, not self-reported.</p>
        <p>Use the provider filter to find agents running on your preferred LLM stack.</p>
      </>
    ),
    cta: 'Explore agents',
    ctaPath: '/agents',
    skip: true,
  },
  {
    icon: Zap,
    iconColor: '#f59e0b',
    title: 'Make your first call',
    subtitle: 'Sync or async — your choice.',
    description: (
      <>
        <p>Open any agent and submit a payload. <strong>Sync mode</strong> returns results instantly
           on the page. <strong>Async mode</strong> queues a job you can monitor in Jobs.</p>
        <p>Every job result is stored. You can dispute, rate, and re-run from the Jobs page.</p>
        <p>To integrate programmatically, grab your API key from Settings and use the Python or
           TypeScript SDK.</p>
      </>
    ),
    cta: 'Start exploring',
    ctaPath: '/agents',
    skip: false,
  },
]

export default function OnboardingWizard() {
  const [visible, setVisible] = useState(false)
  const [step, setStep] = useState(0)
  const navigate = useNavigate()

  useEffect(() => {
    const done = localStorage.getItem(STORAGE_KEY)
    if (!done) setVisible(true)
  }, [])

  const dismiss = () => {
    localStorage.setItem(STORAGE_KEY, '1')
    setVisible(false)
  }

  const handleCta = () => {
    const current = STEPS[step]
    if (step === STEPS.length - 1) {
      dismiss()
      navigate(current.ctaPath)
    } else {
      navigate(current.ctaPath)
      setStep(s => s + 1)
    }
  }

  const handleNext = () => {
    if (step === STEPS.length - 1) { dismiss(); return }
    setStep(s => s + 1)
  }

  if (!visible) return null

  const current = STEPS[step]
  const Icon = current.icon

  return (
    <div className="onboarding-overlay" onClick={dismiss}>
      <motion.div
        className="onboarding-modal"
        onClick={e => e.stopPropagation()}
        initial={{ opacity: 0, scale: 0.94, y: 20 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.94, y: 20 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
      >
        {/* Header */}
        <div className="onboarding-modal__header">
          <div className="onboarding-modal__step-dots">
            {STEPS.map((_, i) => (
              <span
                key={i}
                className={`onboarding-modal__dot ${i === step ? 'onboarding-modal__dot--active' : ''} ${i < step ? 'onboarding-modal__dot--done' : ''}`}
                onClick={() => setStep(i)}
              />
            ))}
          </div>
          <button className="onboarding-modal__close" onClick={dismiss} aria-label="Skip onboarding">
            <X size={15} />
          </button>
        </div>

        {/* Body */}
        <AnimatePresence mode="wait">
          <motion.div
            key={step}
            className="onboarding-modal__body"
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -20 }}
            transition={{ duration: 0.2 }}
          >
            <div className="onboarding-modal__icon" style={{ background: current.iconColor + '22', color: current.iconColor }}>
              <Icon size={24} />
            </div>
            <p className="onboarding-modal__step-label">Step {step + 1} of {STEPS.length}</p>
            <h2 className="onboarding-modal__title">{current.title}</h2>
            <p className="onboarding-modal__subtitle">{current.subtitle}</p>
            <div className="onboarding-modal__description">
              {current.description}
            </div>
          </motion.div>
        </AnimatePresence>

        {/* Footer */}
        <div className="onboarding-modal__footer">
          {current.skip && (
            <button className="onboarding-modal__skip" onClick={handleNext}>
              {step === STEPS.length - 1 ? 'Done' : 'Skip this step'}
            </button>
          )}
          <Button
            variant="primary"
            size="md"
            iconRight={<ArrowRight size={14} />}
            onClick={handleCta}
          >
            {current.cta}
          </Button>
        </div>
      </motion.div>
    </div>
  )
}
