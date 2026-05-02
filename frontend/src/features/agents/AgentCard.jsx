import { useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import AgentSigil from '../../brand/AgentSigil'
import ModelBadge from '../../components/ModelBadge'
import { ArrowRight, AlertTriangle, BadgeCheck } from 'lucide-react'
import './AgentCard.css'

function healthDot(agent) {
  const status = agent.last_health_status
  const checkedAt = agent.last_health_check_at
  if (!status || status === 'unknown') return null
  const ageMs = checkedAt ? Date.now() - new Date(checkedAt).getTime() : Infinity
  const stale = ageMs > 10 * 60 * 1000
  let cls = 'ac__health-dot'
  let title = 'Health unknown'
  if (status === 'healthy' && !stale) {
    cls += ' ac__health-dot--healthy'
    title = `Healthy · checked ${new Date(checkedAt).toLocaleTimeString()}`
  } else if (status === 'unhealthy' || stale) {
    cls += ' ac__health-dot--warn'
    title = stale ? `Last check >10 min ago` : `Unhealthy · last checked ${new Date(checkedAt).toLocaleTimeString()}`
  }
  return <span className={cls} title={title} aria-label={title} />
}

const KIND_LABELS = {
  aztea_built: 'Aztea-built',
  community_skill: 'SKILL.md',
  self_hosted: 'Self-hosted',
}

// Marketplace listing card: renders agent name, description, price, trust signals, and privacy chips.
export default function AgentCard({ agent, index = 0, featured = false, showTrust = false }) {
  const navigate = useNavigate()
  const priceVal = Number(agent.price_per_call_usd ?? 0)
  const price    = `$${priceVal.toFixed(3).replace(/\.?0+$/, '') || '0'}`
  const calls    = agent.total_calls ?? 0
  const trust    = typeof agent.trust_score === 'number' ? Math.round(agent.trust_score) : null
  const highDispute = typeof agent.dispute_rate === 'number' && agent.dispute_rate > 0.10
  const kindLabel = KIND_LABELS[agent.kind] ?? null
  const categoryLabel = String(agent.category || agent.tags?.[0] || 'General')
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, (s) => s.toUpperCase())
  const verified = agent.kind === 'aztea_built'

  const matchReasons = Array.isArray(agent.match_reasons)
    ? agent.match_reasons.filter(r => typeof r === 'string' && r.trim())
    : []
  const showMatch = Boolean(agent._from_search) && matchReasons.length > 0

  return (
    <motion.article
      className={`ac${featured ? ' ac--featured' : ''}`}
      onClick={() => navigate(`/agents/${agent.agent_id}`)}
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.04, 0.28), duration: 0.32, ease: [0.16, 1, 0.3, 1] }}
      layout
      role="button"
      tabIndex={0}
      onKeyDown={e => e.key === 'Enter' && navigate(`/agents/${agent.agent_id}`)}
      aria-label={`Hire ${agent.name}`}
    >
      {/* Header: sigil + name + pills */}
      <div className="ac__head">
        <AgentSigil agentId={agent.agent_id} size="md" className="ac__sigil" />
        <div className="ac__head-meta">
          <div className="ac__eyebrow-row">
            <span className="ac__category-pill">{categoryLabel}</span>
            {verified && <span className="ac__trust-pill"><BadgeCheck size={11} /> Verified</span>}
          </div>
          <p className="ac__name">{agent.name}{healthDot(agent)}</p>
          {agent.model_provider && (
            <ModelBadge provider={agent.model_provider} modelId={agent.model_id} size="xs" />
          )}
        </div>
        <div className="ac__price-block">
          <span className="ac__price">{price}</span>
          <span className="ac__price-label">/ call</span>
        </div>
      </div>

      <p className="ac__desc">{agent.description || 'No description provided.'}</p>

      {showMatch && <p className="ac__match">↳ {matchReasons.slice(0, 2).join(', ')}</p>}
      {highDispute && (
        <p className="ac__warn"><AlertTriangle size={10} />High dispute rate</p>
      )}

      {(agent.job_completion_rate != null || agent.median_latency_seconds != null || agent.jobs_last_30_days > 0) && (
        <div className="ac__reliability">
          {agent.job_completion_rate != null && (
            <span className="ac__stat-chip">{Math.round(agent.job_completion_rate * 100)}% success</span>
          )}
          {agent.median_latency_seconds != null && (
            <span className="ac__stat-chip">~{agent.median_latency_seconds}s</span>
          )}
          {agent.jobs_last_30_days > 0 && (
            <span className="ac__stat-chip">{agent.jobs_last_30_days} jobs/30d</span>
          )}
        </div>
      )}

      {kindLabel && !verified && (
        <span className={`ac__kind-chip ac__kind-chip--${agent.kind}`}>{kindLabel}</span>
      )}

      <div className="ac__foot">
        <span className="ac__calls">
          {calls > 0 ? `${calls.toLocaleString()} calls` : 'New'}
        </span>
        {showTrust && trust != null && (
          <span className="ac__trust" title="Trust score (0–100)">
            <span className="ac__trust-val">{trust}</span>
            <span className="ac__trust-lbl">trust</span>
          </span>
        )}
        <span className="ac__cta">
          Hire <ArrowRight size={11} strokeWidth={2.5} />
        </span>
      </div>
    </motion.article>
  )
}
