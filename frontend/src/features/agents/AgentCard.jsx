import { useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import AgentSigil from '../../brand/AgentSigil'
import { getAgentColor } from '../../brand/sigilTraits'
import Pill from '../../ui/Pill'
import ModelBadge from '../../components/ModelBadge'
import { ArrowRight, AlertTriangle, Zap } from 'lucide-react'
import './AgentCard.css'

export default function AgentCard({ agent, index = 0 }) {
  const navigate    = useNavigate()
  const accentColor = getAgentColor(agent.agent_id)

  const successPct  = agent.success_rate  != null ? Math.round(agent.success_rate * 100) : null
  const trustScore  = typeof agent.trust_score === 'number' ? Math.round(agent.trust_score) : null
  const highDispute = typeof agent.dispute_rate === 'number' && agent.dispute_rate > 0.10
  const price       = `$${Number(agent.price_per_call_usd ?? 0).toFixed(2)}`
  const calls       = agent.total_calls ?? 0

  const matchReasons = Array.isArray(agent.match_reasons)
    ? agent.match_reasons.filter(r => typeof r === 'string' && r.trim())
    : []
  const showMatch = Boolean(agent._from_search) && matchReasons.length > 0

  const trustColor = trustScore == null ? null
    : trustScore >= 80 ? 'var(--positive, #22c55e)'
    : trustScore >= 55 ? '#d97706'
    : '#dc2626'

  return (
    <motion.article
      className="ac"
      onClick={() => navigate(`/agents/${agent.agent_id}`)}
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.04, 0.28), duration: 0.32, ease: [0.16, 1, 0.3, 1] }}
      layout
      role="button"
      tabIndex={0}
      onKeyDown={e => e.key === 'Enter' && navigate(`/agents/${agent.agent_id}`)}
      aria-label={`Open ${agent.name}`}
      style={{ '--ac-accent': accentColor }}
    >
      {/* Left accent stripe */}
      <div className="ac__stripe" />

      {/* Header: sigil + name + price */}
      <div className="ac__head">
        <AgentSigil agentId={agent.agent_id} size="md" className="ac__sigil" />
        <div className="ac__head-meta">
          <p className="ac__name">{agent.name}</p>
          <div className="ac__subrow">
            <span className="ac__price">{price}</span>
            {trustScore != null && (
              <span className="ac__trust" style={{ color: trustColor }}>
                ★ {trustScore}
              </span>
            )}
            {agent.verified && (
              <span className="ac__verified" title="Verified agent">
                <Zap size={9} fill="currentColor" />
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Description */}
      <p className="ac__desc">{agent.description || 'No description provided.'}</p>

      {/* Semantic match */}
      {showMatch && (
        <p className="ac__match">↳ {matchReasons.slice(0, 2).join(', ')}</p>
      )}

      {/* High dispute warning */}
      {highDispute && (
        <p className="ac__warn">
          <AlertTriangle size={10} />
          High dispute rate
        </p>
      )}

      {/* Tags */}
      {((agent.tags ?? []).length > 0 || agent.model_provider) && (
        <div className="ac__tags">
          {(agent.tags ?? []).slice(0, 3).map(t => <Pill key={t} size="sm">{t}</Pill>)}
          {agent.model_provider && (
            <ModelBadge provider={agent.model_provider} modelId={agent.model_id} />
          )}
        </div>
      )}

      {/* Footer */}
      <div className="ac__foot">
        {successPct != null ? (
          <div className="ac__rel">
            <div className="ac__rel-track">
              <motion.div
                className="ac__rel-fill"
                initial={{ width: 0 }}
                animate={{ width: `${successPct}%` }}
                transition={{ duration: 0.9, ease: [0.16, 1, 0.3, 1], delay: Math.min(index * 0.04, 0.28) + 0.1 }}
              />
            </div>
            <span className="ac__rel-pct">{successPct}%</span>
          </div>
        ) : (
          <span className="ac__calls">
            {calls > 0 ? `${calls.toLocaleString()} calls` : 'New'}
          </span>
        )}
        <span className="ac__cta">
          Open <ArrowRight size={11} strokeWidth={2.5} />
        </span>
      </div>
    </motion.article>
  )
}
