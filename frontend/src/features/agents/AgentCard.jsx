import { useNavigate } from 'react-router-dom'
import { motion } from 'motion/react'
import AgentSigil from '../../brand/AgentSigil'
import Pill from '../../ui/Pill'
import ModelBadge from '../../components/ModelBadge'
import { ArrowRight, AlertTriangle, BookOpen } from 'lucide-react'
import './AgentCard.css'

function healthDot(agent) {
  const status = agent.last_health_status
  const checkedAt = agent.last_health_check_at
  if (!status || status === 'unknown') return null
  const ageMs = checkedAt ? Date.now() - new Date(checkedAt).getTime() : Infinity
  const stale = ageMs > 10 * 60 * 1000 // >10 min
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
export default function AgentCard({ agent, index = 0, showTrust = false }) {
  const navigate = useNavigate()
  const price    = `$${Number(agent.price_per_call_usd ?? 0).toFixed(2)}`
  const calls    = agent.total_calls ?? 0
  const trust    = typeof agent.trust_score === 'number'
    ? Math.round(agent.trust_score)
    : null
  const highDispute = typeof agent.dispute_rate === 'number' && agent.dispute_rate > 0.10
  const exampleCount = Array.isArray(agent.output_examples) ? agent.output_examples.length : 0
  const kindLabel = KIND_LABELS[agent.kind] ?? null
  const privacyChips = [
    agent.pii_safe ? 'PII-safe' : null,
    agent.outputs_not_stored ? 'No output storage' : null,
    agent.audit_logged ? 'Audit logged' : null,
    agent.region_locked ? `Region ${String(agent.region_locked).toUpperCase()}` : null,
  ].filter(Boolean)
  const topClientTrust = agent.by_client && typeof agent.by_client === 'object'
    ? Object.entries(agent.by_client)
      .filter(([, score]) => typeof score === 'number')
      .sort((a, b) => b[1] - a[1])[0]
    : null

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
          <p className="ac__name">{agent.name}{healthDot(agent)}</p>
          <div className="ac__head-sub">
            <span className="ac__price">{price}</span>
            {agent.model_provider && (
              <ModelBadge provider={agent.model_provider} modelId={agent.model_id} size="xs" />
            )}
          </div>
        </div>
        {kindLabel && (
          <span className={`ac__kind-chip ac__kind-chip--${agent.kind}`}>{kindLabel}</span>
        )}
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

      {privacyChips.length > 0 && (
        <div className="ac__policy-row">
          {privacyChips.slice(0, 3).map(chip => (
            <span key={chip} className="ac__policy-chip">{chip}</span>
          ))}
        </div>
      )}

      {/* Reliability stats */}
      {(agent.jobs_last_30_days > 0 || agent.job_completion_rate != null || agent.median_latency_seconds != null) && (
        <div className="ac__reliability">
          {agent.jobs_last_30_days > 0 && (
            <span className="ac__stat-chip">{agent.jobs_last_30_days} jobs/30d</span>
          )}
          {agent.job_completion_rate != null && (
            <span className="ac__stat-chip">{Math.round(agent.job_completion_rate * 100)}% success</span>
          )}
          {agent.median_latency_seconds != null && (
            <span className="ac__stat-chip">~{agent.median_latency_seconds}s</span>
          )}
          {topClientTrust && (
            <span className="ac__stat-chip">
              {topClientTrust[0]} {Math.round(topClientTrust[1])}
            </span>
          )}
        </div>
      )}

      {/* Footer */}
      <div className="ac__foot">
        <span className="ac__calls">
          {calls > 0 ? `${calls.toLocaleString()} calls` : 'New'}
        </span>
        {showTrust && trust != null && (
          <span className="ac__trust" title="Trust score (0–100)">★ {trust}</span>
        )}
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
