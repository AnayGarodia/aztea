import { useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import AgentSigil from '../../brand/AgentSigil'
import Pill from '../../ui/Pill'
import ModelBadge from '../../components/ModelBadge'
import { ArrowRight, AlertTriangle, BookOpen } from 'lucide-react'
import './AgentCard.css'

export default function AgentCard({ agent, index = 0 }) {
  const navigate = useNavigate()
  const price    = `$${Number(agent.price_per_call_usd ?? 0).toFixed(2)}`
  const calls    = agent.total_calls ?? 0
  const highDispute = typeof agent.dispute_rate === 'number' && agent.dispute_rate > 0.10
  const exampleCount = Array.isArray(agent.output_examples) ? agent.output_examples.length : 0

  const matchReasons = Array.isArray(agent.match_reasons)
    ? agent.match_reasons.filter(r => typeof r === 'string' && r.trim())
    : []
  const showMatch = Boolean(agent._from_search) && matchReasons.length > 0

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
    >
      {/* Header: sigil + name + price */}
      <div className="ac__head">
        <AgentSigil agentId={agent.agent_id} size="md" className="ac__sigil" />
        <div className="ac__head-meta">
          <p className="ac__name">{agent.name}</p>
          <div className="ac__head-sub">
            <span className="ac__price">{price}</span>
            {agent.model_provider && (
              <ModelBadge provider={agent.model_provider} modelId={agent.model_id} size="xs" />
            )}
          </div>
        </div>
      </div>

      {/* Description */}
      <p className="ac__desc">{agent.description || 'No description provided.'}</p>

      {showMatch && <p className="ac__match">↳ {matchReasons.slice(0, 2).join(', ')}</p>}
      {highDispute && (
        <p className="ac__warn"><AlertTriangle size={10} />High dispute rate</p>
      )}

      {/* Tags */}
      {(agent.tags ?? []).length > 0 && (
        <div className="ac__tags">
          {(agent.tags ?? []).slice(0, 3).map(t => <Pill key={t} size="sm">{t}</Pill>)}
        </div>
      )}

      {/* Footer */}
      <div className="ac__foot">
        <span className="ac__calls">
          {calls > 0 ? `${calls.toLocaleString()} calls` : 'New'}
        </span>
        {exampleCount > 0 && (
          <span className="ac__examples" title="Work examples available">
            <BookOpen size={10} strokeWidth={2} />
            {exampleCount}
          </span>
        )}
        <span className="ac__cta">
          Open <ArrowRight size={11} strokeWidth={2.5} />
        </span>
      </div>
    </motion.article>
  )
}
