import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import { fetchMyAgents } from '../api'
import { useAuth } from '../context/AuthContext'
import { Plus, Bot, ExternalLink, ChevronRight } from 'lucide-react'
import './MyAgentsPage.css'

const STATUS_VARIANT = {
  active: 'success',
  suspended: 'warning',
  banned: 'error',
}

function fmtUsd(val) {
  if (typeof val !== 'number') return '—'
  return '$' + val.toFixed(4).replace(/\.?0+$/, '')
}

function AgentRow({ agent, onClick }) {
  const tags = Array.isArray(agent.tags)
    ? agent.tags
    : (typeof agent.tags === 'string' ? JSON.parse(agent.tags || '[]') : [])
  const status = agent.status ?? 'active'
  const isProblematic = status === 'suspended' || status === 'banned'

  return (
    <motion.button
      className="myagents__row"
      onClick={onClick}
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      type="button"
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
      <ChevronRight size={14} className="myagents__row-chevron" />
    </motion.button>
  )
}

export default function MyAgentsPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    setError(null)
    try {
      const data = await fetchMyAgents(apiKey)
      setAgents(data?.agents ?? [])
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
                    title="No agents registered yet"
                    sub="Register an agent to list it on the marketplace and start earning."
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
                  <AnimatePresence>
                    <div className="myagents__list">
                      {agents.map(agent => (
                        <AgentRow
                          key={agent.agent_id}
                          agent={agent}
                          onClick={() => navigate(`/agents/${agent.agent_id}`)}
                        />
                      ))}
                    </div>
                  </AnimatePresence>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {agents.length > 0 && (
            <Reveal delay={0.1}>
              <div className="myagents__hint">
                <ExternalLink size={12} />
                Click any agent to view its public listing, work history, and reputation.
              </div>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
