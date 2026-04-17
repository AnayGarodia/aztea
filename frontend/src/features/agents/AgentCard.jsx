import { useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import AgentSigil from '../../brand/AgentSigil'
import Pill from '../../ui/Pill'
import Spotlight from '../../ui/motion/Spotlight'
import ModelBadge from '../../components/ModelBadge'
import './AgentCard.css'

export default function AgentCard({ agent, index = 0 }) {
  const navigate = useNavigate()

  const successPct  = agent.success_rate != null ? Math.round(agent.success_rate * 100) : null
  const latency     = agent.avg_latency_ms != null ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—'
  const calls       = agent.total_calls ?? 0
  const trustScore  = typeof agent.trust_score === 'number' ? agent.trust_score.toFixed(0) : null
  const disputeRate = typeof agent.dispute_rate === 'number' ? agent.dispute_rate : null
  const highDispute = disputeRate !== null && disputeRate > 0.10
  const avgRating   = typeof agent.quality_rating_avg === 'number' ? agent.quality_rating_avg : null
  const ratingCount = agent.quality_rating_count ?? 0
  const matchReasons = Array.isArray(agent.match_reasons)
    ? agent.match_reasons.map(r => (typeof r === 'string' ? r.trim() : '')).filter(Boolean)
    : []
  const showMatchReasons = Boolean(agent._from_search) && matchReasons.length > 0

  const handleClick = () => navigate(`/agents/${agent.agent_id}`)

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05, duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
      layout
    >
      <Spotlight color="var(--accent-glow)">
        <div
          className="agent-card"
          onClick={handleClick}
          role="button"
          tabIndex={0}
          onKeyDown={e => e.key === 'Enter' && handleClick()}
          aria-label={`Open ${agent.name} profile`}
        >
          {/* Sigil header */}
          <div className="agent-card__sigil-area">
            <AgentSigil agentId={agent.agent_id} size="md" />
            <div className="agent-card__header-right">
              {trustScore !== null && (
                <span className="agent-card__trust" title="Trust score (0–100)">
                  ★ {trustScore}
                </span>
              )}
              <span className="agent-card__price t-mono">
                ${Number(agent.price_per_call_usd).toFixed(2)}
              </span>
            </div>
          </div>

          {/* Info section */}
          <div className="agent-card__body">
            <p className="agent-card__name">{agent.name}</p>

            <p className="agent-card__desc">{agent.description || 'No description provided.'}</p>

            {showMatchReasons && (
              <p className="agent-card__reason" title={matchReasons.join(', ')}>
                matched: {matchReasons.slice(0, 2).join(', ')}
              </p>
            )}

            <div className="agent-card__tags">
              {(agent.tags ?? []).slice(0, 3).map(t => <Pill key={t} size="sm">{t}</Pill>)}
              {agent.model_provider && (
                <ModelBadge provider={agent.model_provider} modelId={agent.model_id} />
              )}
            </div>

            {/* Reliability bar */}
            {successPct !== null && (
              <div className="agent-card__rel">
                <div className="agent-card__rel-row">
                  <span className="agent-card__rel-label">Reliability</span>
                  <span className="agent-card__rel-pct">{successPct}%</span>
                </div>
                <div className="agent-card__rel-track">
                  <motion.div
                    className="agent-card__rel-fill"
                    initial={{ width: 0 }}
                    animate={{ width: `${successPct}%` }}
                    transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1], delay: index * 0.05 + 0.2 }}
                  />
                </div>
              </div>
            )}

            <div className="agent-card__meta">
              <div className="agent-card__meta-item">
                <span className="agent-card__meta-val t-mono">{latency}</span>
                <span className="agent-card__meta-label">Latency</span>
              </div>
              <div className="agent-card__meta-item">
                <span className="agent-card__meta-val t-mono">{calls.toLocaleString()}</span>
                <span className="agent-card__meta-label">Calls</span>
              </div>
              {avgRating !== null && ratingCount > 0 && (
                <div className="agent-card__meta-item" title={`${ratingCount} rating${ratingCount !== 1 ? 's' : ''}`}>
                  <span className="agent-card__meta-val t-mono">{'★'.repeat(Math.round(avgRating))}{'☆'.repeat(5 - Math.round(avgRating))}</span>
                  <span className="agent-card__meta-label">Rating</span>
                </div>
              )}
              <div className="agent-card__meta-cta">
                Open →
              </div>
            </div>
            {highDispute && (
              <p className="agent-card__dispute-warn" title={`${(disputeRate * 100).toFixed(1)}% dispute rate`}>
                ⚠ High dispute rate
              </p>
            )}
          </div>
        </div>
      </Spotlight>
    </motion.div>
  )
}
