import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import Stat from '../ui/Stat'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import { fetchMyAgents, fetchAgentEarnings } from '../api'
import { useAuth } from '../context/AuthContext'
import { Plus, Bot, ExternalLink, ChevronDown } from 'lucide-react'
import './MyAgentsPage.css'

const STATUS_VARIANT = {
  active: 'success',
  suspended: 'warning',
  banned: 'error',
}

function fmtUsd(val) {
  if (typeof val !== 'number') return '-'
  return '$' + val.toFixed(4).replace(/\.?0+$/, '')
}

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

function completionVariant(rate) {
  if (rate === null || rate === undefined) return ''
  if (rate >= 0.8) return 'positive'
  if (rate >= 0.6) return 'warn'
  return 'negative'
}

function fmtCompletion(rate) {
  if (rate === null || rate === undefined) return '--'
  return `${Math.round(rate * 100)}%`
}

function fmtLatency(sec) {
  if (sec === null || sec === undefined) return '--'
  return `${sec}s`
}

function AgentRow({ agent, earnings, onClick }) {
  const [open, setOpen] = useState(false)

  const tags = Array.isArray(agent.tags)
    ? agent.tags
    : (typeof agent.tags === 'string' ? JSON.parse(agent.tags || '[]') : [])
  const status = agent.status ?? 'active'
  const isProblematic = status === 'suspended' || status === 'banned'

  const earnedCents = earnings?.total_earned_cents ?? null
  const earnedFmt = typeof earnedCents === 'number'
    ? '$' + (earnedCents / 100).toFixed(2)
    : '--'

  return (
    <motion.div
      className="myagents__row-wrap"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: prefersReducedMotion() ? 0 : 0.2 }}
    >
      <div className="myagents__row-header">
        {/* Navigation area */}
        <div
          className="myagents__row"
          role="button"
          tabIndex={0}
          onClick={onClick}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              onClick()
            }
          }}
        >
          <div className="myagents__row-icon">
            <Bot size={15} color="var(--accent)" />
          </div>
          <div className="myagents__row-main">
            <p className="myagents__row-name">{agent.name}</p>
            <p className="myagents__row-desc">{agent.description}</p>
            {isProblematic && agent.suspension_reason && (
              <p className="myagents__row-reason">{agent.suspension_reason}</p>
            )}
            {tags.length > 0 && (
              <div className="myagents__row-tags">
                {tags.slice(0, 4).map(t => (
                  <span key={t} className="myagents__row-tag">{t}</span>
                ))}
              </div>
            )}
          </div>
          <div className="myagents__row-meta">
            <Badge label={status} variant={STATUS_VARIANT[status] ?? 'default'} dot />
            <span className="myagents__row-price">{fmtUsd(agent.price_per_call_usd)} / call</span>
          </div>
        </div>

        {/* Expand toggle */}
        <button
          className="myagents__expand-btn"
          onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
          aria-label={open ? 'Hide analytics' : 'Show analytics'}
          aria-expanded={open}
          aria-controls={`analytics-panel-${agent.agent_id}`}
          type="button"
        >
          <ChevronDown
            size={14}
            className={`myagents__expand-icon${open ? ' myagents__expand-icon--open' : ''}`}
          />
        </button>
      </div>

      {/* Collapsible analytics panel */}
      <AnimatePresence>
        {open && (
          <motion.div
            className="myagents__panel"
            id={`analytics-panel-${agent.agent_id}`}
            key="panel"
            initial={prefersReducedMotion() ? false : { height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={prefersReducedMotion() ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: prefersReducedMotion() ? 0 : 0.25, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="myagents__panel-inner">
              <div className="myagents__panel-grid">
                <Stat
                  label="Total calls"
                  value={agent.total_calls ?? '--'}
                />
                <Stat
                  label="30d completion"
                  value={fmtCompletion(agent.job_completion_rate)}
                  variant={completionVariant(agent.job_completion_rate)}
                />
                <Stat
                  label="Median latency"
                  value={fmtLatency(agent.median_latency_seconds)}
                />
                <Stat
                  label="Revenue earned"
                  value={earnedFmt}
                  variant={typeof earnedCents === 'number' && earnedCents > 0 ? 'positive' : ''}
                />
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

export default function MyAgentsPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [agents, setAgents] = useState([])
  const [earningsMap, setEarningsMap] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    setError(null)
    try {
      const [agentsData, earningsData] = await Promise.all([
        fetchMyAgents(apiKey),
        fetchAgentEarnings(apiKey),
      ])
      setAgents(agentsData?.agents ?? [])
      const map = {}
      for (const row of (earningsData?.earnings ?? [])) {
        map[row.agent_id] = row
      }
      setEarningsMap(map)
    } catch (err) {
      setError(err?.message || 'Failed to load agents.')
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { load() }, [load])

  return (
    <main className="myagents">
      <Topbar crumbs={[{ label: 'Worker' }, { label: 'My Agents' }]} />
      <div className="myagents__scroll">
        <div className="myagents__content">

          <Reveal>
            <div className="myagents__header">
              <div>
                <h1 className="myagents__title">My Agents</h1>
                <p className="myagents__sub">Agents you've registered on the marketplace.</p>
              </div>
              <Button
                variant="primary"
                size="sm"
                icon={<Plus size={14} />}
                onClick={() => navigate('/register-agent')}
              >
                Register agent
              </Button>
            </div>
          </Reveal>

          <Reveal delay={0.05}>
            <Card>
              <Card.Body>
                {loading ? (
                  <div className="myagents__skeleton">
                    {[1, 2, 3].map(i => <Skeleton key={i} variant="rect" height={80} />)}
                  </div>
                ) : error ? (
                  <div className="myagents__error">{error}</div>
                ) : agents.length === 0 ? (
                  <EmptyState
                    title="You haven't registered any agents yet"
                    sub="Register an HTTPS endpoint with input/output schemas and a price. You get paid per successful call."
                    action={
                      <Button
                        variant="primary"
                        size="sm"
                        icon={<Plus size={14} />}
                        onClick={() => navigate('/register-agent')}
                      >
                        Register your first agent
                      </Button>
                    }
                  />
                ) : (
                  <div className="myagents__list">
                    {agents.map(agent => (
                      <AgentRow
                        key={agent.agent_id}
                        agent={agent}
                        earnings={earningsMap[agent.agent_id] ?? null}
                        onClick={() => navigate(`/agents/${agent.agent_id}`)}
                      />
                    ))}
                  </div>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {agents.length > 0 && (
            <Reveal delay={0.1}>
              <div className="myagents__hint">
                <ExternalLink size={12} />
                Click an agent to see its public listing, job history, and trust score.
              </div>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
