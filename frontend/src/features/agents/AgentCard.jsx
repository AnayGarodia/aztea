import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import AgentCharacter from '../../brand/AgentCharacter'
import { generateAgentCharacter } from '../../brand/characterUtils'
import Pill from '../../ui/Pill'
import './AgentCard.css'

export default function AgentCard({ agent, index = 0 }) {
  const navigate = useNavigate()
  const traits = generateAgentCharacter(agent.agent_id)
  const [charState, setCharState] = useState('idle')
  const [isHovered, setIsHovered] = useState(false)

  const successPct = agent.success_rate != null ? Math.round(agent.success_rate * 100) : null
  const latency    = agent.avg_latency_ms != null ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—'
  const calls      = agent.total_calls ?? 0
  const matchReasons = Array.isArray(agent.match_reasons)
    ? agent.match_reasons
      .map(reason => (typeof reason === 'string' ? reason.trim() : ''))
      .filter(Boolean)
    : []
  const showMatchReasons = Boolean(agent._from_search) && matchReasons.length > 0

  const handleMouseEnter = () => {
    setIsHovered(true)
    setCharState('working')
  }
  const handleMouseLeave = () => {
    setIsHovered(false)
    setCharState('idle')
  }
  const handleClick = () => {
    navigate(`/agents/${agent.agent_id}`)
  }

  return (
    <motion.div
      className="agent-card"
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.06, duration: 0.35 }}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
      onClick={handleClick}
      role="button"
      tabIndex={0}
      onKeyDown={e => e.key === 'Enter' && handleClick()}
      aria-label={`Open ${agent.name} profile`}
    >
      {/* Character section */}
      <div className="agent-card__char-area">
        <motion.div
          animate={{ scale: isHovered ? 1.06 : 1 }}
          transition={{ type: 'spring', stiffness: 300, damping: 20 }}
        >
          <AgentCharacter
            {...traits}
            state={charState}
            size={80}
          />
        </motion.div>
      </div>

      {/* Info section */}
      <div className="agent-card__body">
        <div className="agent-card__header">
          <p className="agent-card__name">{agent.name}</p>
          <span
            className="agent-card__price"
            style={{ background: traits.bodyColor + '22', color: traits.bodyColor, borderColor: traits.bodyColor + '55' }}
          >
            ${Number(agent.price_per_call_usd).toFixed(2)} / call
          </span>
        </div>

        <p className="agent-card__desc">{agent.description || 'No description provided yet.'}</p>
        {showMatchReasons && (
          <p className="agent-card__reason" title={matchReasons.join(', ')}>
            matched: {matchReasons.join(', ')}
          </p>
        )}

        <div className="agent-card__tags">
          {(agent.tags ?? []).slice(0, 3).map(t => <Pill key={t} size="sm">{t}</Pill>)}
        </div>

        {/* XP bar — success rate as game progress bar */}
        {successPct !== null && (
          <div className="agent-card__xp">
            <div className="agent-card__xp-header">
              <span className="agent-card__xp-label">Reliability</span>
              <span className="agent-card__xp-pct" style={{ color: traits.bodyColor }}>{successPct}%</span>
            </div>
            <div className="agent-card__xp-track">
              <motion.div
                className="agent-card__xp-fill"
                style={{ background: traits.bodyColor }}
                initial={{ width: 0 }}
                animate={{ width: `${successPct}%` }}
                transition={{ duration: 0.8, ease: 'easeOut', delay: index * 0.06 + 0.2 }}
              />
            </div>
          </div>
        )}

        <div className="agent-card__meta">
          <div className="agent-card__meta-item">
            <span className="agent-card__meta-val">{latency}</span>
            <span className="agent-card__meta-label">Latency</span>
          </div>
          <div className="agent-card__meta-item">
            <span className="agent-card__meta-val">{calls.toLocaleString()}</span>
            <span className="agent-card__meta-label">Calls</span>
          </div>
          <div className="agent-card__meta-item agent-card__meta-item--cta">
            <span className="agent-card__meta-label">Open profile →</span>
          </div>
        </div>
      </div>
    </motion.div>
  )
}
