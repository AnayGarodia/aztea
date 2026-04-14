import { Link } from 'react-router-dom'
import { motion } from 'framer-motion'
import Pill from '../../ui/Pill'
import './AgentCard.css'

export default function AgentCard({ agent, index = 0 }) {
  const successPct = agent.success_rate != null ? `${Math.round(agent.success_rate * 100)}%` : '—'
  const latency = agent.avg_latency_ms != null ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—'
  const calls = agent.total_calls ?? 0

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05, duration: 0.3 }}
    >
      <Link to={`/agents/${agent.agent_id}`} className="agent-card">
        <div className="agent-card__header">
          <p className="agent-card__name">{agent.name}</p>
          <span className="agent-card__price">${Number(agent.price_per_call_usd).toFixed(2)}</span>
        </div>
        <p className="agent-card__desc">{agent.description}</p>
        <div className="agent-card__tags">
          {(agent.tags ?? []).slice(0, 4).map(t => <Pill key={t} size="sm">{t}</Pill>)}
        </div>
        <div className="agent-card__meta">
          <div className="agent-card__meta-item">
            <span className="agent-card__meta-val">{successPct}</span>
            <span className="agent-card__meta-label">Success</span>
          </div>
          <div className="agent-card__meta-item">
            <span className="agent-card__meta-val">{latency}</span>
            <span className="agent-card__meta-label">Avg latency</span>
          </div>
          <div className="agent-card__meta-item">
            <span className="agent-card__meta-val">{calls.toLocaleString()}</span>
            <span className="agent-card__meta-label">Total calls</span>
          </div>
        </div>
      </Link>
    </motion.div>
  )
}
