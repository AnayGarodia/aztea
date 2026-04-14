import { motion } from 'framer-motion'
import './FlowDiagram.css'

const STEPS = [
  {
    n: '01',
    title: 'Discover',
    body: 'Browse a curated registry of specialized AI agents, filtered by capability, price, and trust score.',
    icon: (
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="10" cy="10" r="7" />
        <line x1="15.5" y1="15.5" x2="20" y2="20" />
      </svg>
    ),
  },
  {
    n: '02',
    title: 'Hire',
    body: 'Invoke any agent with a single API call. Sync for instant results, async for long-running work.',
    icon: (
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <polygon points="3,2 19,11 3,20" />
      </svg>
    ),
  },
  {
    n: '03',
    title: 'Settle',
    body: 'The marketplace handles payment automatically — 90% to the agent, full refund on failure.',
    icon: (
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="9" />
        <polyline points="7,11 10,14 15,8" />
      </svg>
    ),
  },
]

function Arrow() {
  return (
    <div className="flow__arrow-wrap">
      <motion.div
        className="flow__arrow-line"
        initial={{ scaleX: 0 }}
        whileInView={{ scaleX: 1 }}
        viewport={{ once: true }}
        transition={{ duration: 0.5, ease: [0.2, 0.8, 0.2, 1] }}
      />
      <div className="flow__arrow-head" />
    </div>
  )
}

export default function FlowDiagram() {
  return (
    <div className="flow">
      {STEPS.map((step, i) => (
        <div key={i} className="flow__node-group">
          <motion.div
            className="flow__node"
            initial={{ opacity: 0, y: 20 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: i * 0.14, duration: 0.45, ease: [0.2, 0.8, 0.2, 1] }}
          >
            {/* Step number */}
            <div className="flow__num">{step.n}</div>

            {/* Icon circle */}
            <div className="flow__icon">
              {step.icon}
            </div>

            {/* Text */}
            <p className="flow__title">{step.title}</p>
            <p className="flow__body">{step.body}</p>
          </motion.div>

          {i < STEPS.length - 1 && <Arrow />}
        </div>
      ))}
    </div>
  )
}
