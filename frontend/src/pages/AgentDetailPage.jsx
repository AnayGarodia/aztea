import { useMemo, useState, useEffect, useCallback } from 'react'
import { useParams, Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Pill from '../ui/Pill'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import AgentSigil from '../brand/AgentSigil'
import { getAgentColor } from '../brand/sigilTraits'
import AgentInputForm from '../features/agents/AgentInputForm'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import TrustGauge from '../features/agents/TrustGauge'
import { callAgent, createJob, fetchAgentWorkHistory } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowLeft, ArrowUpRight, AlertTriangle, Zap, Clock, BarChart2, Shield, ChevronDown, ChevronUp, BookOpen, Lock } from 'lucide-react'
import ModelBadge from '../components/ModelBadge'
import './AgentDetailPage.css'

function fmtPct(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function fmtMs(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${Math.round(value)} ms`
}

export default function AgentDetailPage() {
  const { id } = useParams()
  const { agents, wallet, apiKey, showToast, refreshJobs } = useMarket()
  const [mode, setMode] = useState('sync')
  const [invokeLoading, setInvokeLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [jobInfo, setJobInfo] = useState(null)
  const [workHistory, setWorkHistory] = useState(null)
  const [workHistoryLoading, setWorkHistoryLoading] = useState(false)
  const [workHistoryOffset, setWorkHistoryOffset] = useState(0)
  const [workHistoryTotal, setWorkHistoryTotal] = useState(0)
  const [expandedExample, setExpandedExample] = useState(null)

  const agent = useMemo(() => agents.find(a => a.agent_id === id), [agents, id])

  const loadWorkHistory = useCallback(async (offset = 0) => {
    if (!agent || !apiKey) return
    setWorkHistoryLoading(true)
    try {
      const data = await fetchAgentWorkHistory(apiKey, agent.agent_id, { limit: 10, offset })
      if (offset === 0) {
        setWorkHistory(data?.items ?? [])
      } else {
        setWorkHistory(prev => [...(prev ?? []), ...(data?.items ?? [])])
      }
      setWorkHistoryTotal(data?.total ?? 0)
      setWorkHistoryOffset(offset)
    } catch {
      // non-fatal
    } finally {
      setWorkHistoryLoading(false)
    }
  }, [agent, apiKey])

  useEffect(() => {
    if (agent) loadWorkHistory(0)
  }, [agent?.agent_id]) // eslint-disable-line react-hooks/exhaustive-deps

  const accentColor = agent ? getAgentColor(agent.agent_id) : null
  const priceCents = agent ? Math.round((agent.price_per_call_usd ?? 0) * 100) : 0
  const balanceCents = wallet?.balance_cents ?? 0
  const insufficientBalance = priceCents > 0 && balanceCents < priceCents
  const outputExamples = Array.isArray(agent?.output_examples) ? agent.output_examples : []
  const trustScore = typeof agent?.trust_score === 'number' ? Math.round(agent.trust_score) : null
  const successPct = agent?.success_rate != null ? Math.round(agent.success_rate * 100) : null
  const highDispute = typeof agent?.dispute_rate === 'number' && agent.dispute_rate > 0.10

  const handleInvoke = async (payload, { privateTask = false } = {}) => {
    if (!agent) return
    setInvokeLoading(true)
    setResult(null)
    setJobInfo(null)
    try {
      if (mode === 'async') {
        const job = await createJob(apiKey, agent.agent_id, payload, 3, { privateTask })
        setJobInfo({ jobId: job.job_id, status: job.status })
        showToast?.(`Job queued — ${job.job_id.slice(0, 8)}`, 'success')
        await refreshJobs?.()
        return
      }
      const response = await callAgent(apiKey, agent.agent_id, payload, { privateTask })
      setResult(response.body)
      if (!response.ok) showToast?.(`Call failed (${response.status})`, 'error')
      else if (!privateTask) setTimeout(() => loadWorkHistory(0), 1500)
    } catch (err) {
      showToast?.(err?.message ?? 'Invoke failed', 'error')
    } finally {
      setInvokeLoading(false)
    }
  }

  if (!agent) {
    return (
      <main className="agent-detail">
        <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: 'Agent' }]} />
        <div className="agent-detail__scroll">
          <EmptyState
            title="Agent not found"
            sub="This agent may have been removed from the registry."
            action={
              <Link to="/agents">
                <Button variant="secondary" icon={<ArrowLeft size={14} />}>Back to agents</Button>
              </Link>
            }
          />
        </div>
      </main>
    )
  }

  return (
    <main className="agent-detail">
      <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: agent.name }]} />

      <div className="agent-detail__scroll">
        <div className="agent-detail__content">

          {/* Hero */}
          <Reveal>
            <div className="agent-detail__hero" style={{ '--ad-accent': accentColor }}>
              <div className="agent-detail__hero-accent" />
              <div className="agent-detail__hero-row">
                <AgentSigil agentId={agent.agent_id} size="lg" className="agent-detail__sigil" />
                <div className="agent-detail__hero-meta">
                  <div className="agent-detail__hero-name-row">
                    <h1 className="agent-detail__name">{agent.name}</h1>
                    {agent.verified && (
                      <span className="agent-detail__verified-badge" title="Verified agent">
                        <Zap size={12} fill="currentColor" /> Verified
                      </span>
                    )}
                    {highDispute && (
                      <span className="agent-detail__dispute-badge">
                        <AlertTriangle size={11} /> High disputes
                      </span>
                    )}
                  </div>
                  {agent.description && (
                    <p className="agent-detail__description">{agent.description}</p>
                  )}
                  <div className="agent-detail__hero-foot">
                    {(agent.tags ?? []).length > 0 && (
                      <div className="agent-detail__tags">
                        {agent.tags.map(t => <Pill key={t} size="sm">{t}</Pill>)}
                        {agent.model_provider && (
                          <ModelBadge provider={agent.model_provider} modelId={agent.model_id} />
                        )}
                      </div>
                    )}
                    <div className="agent-detail__inline-stats">
                      <span className="agent-detail__inline-stat">
                        <span className="agent-detail__inline-stat-val">
                          ${Number(agent.price_per_call_usd).toFixed(2)}
                        </span>
                        <span className="agent-detail__inline-stat-lbl">per call</span>
                      </span>
                      {successPct != null && (
                        <span className="agent-detail__inline-stat">
                          <span className="agent-detail__inline-stat-val agent-detail__inline-stat-val--green">
                            {successPct}%
                          </span>
                          <span className="agent-detail__inline-stat-lbl">success</span>
                        </span>
                      )}
                      {trustScore != null && (
                        <span className="agent-detail__inline-stat">
                          <span className="agent-detail__inline-stat-val">★ {trustScore}</span>
                          <span className="agent-detail__inline-stat-lbl">trust</span>
                        </span>
                      )}
                      {agent.total_calls > 0 && (
                        <span className="agent-detail__inline-stat">
                          <span className="agent-detail__inline-stat-val">
                            {(agent.total_calls ?? 0).toLocaleString()}
                          </span>
                          <span className="agent-detail__inline-stat-lbl">calls</span>
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </Reveal>

          {/* Trust gauge */}
          <Reveal delay={0.08}>
            <div className="agent-detail__trust">
              <p className="agent-detail__trust-title">Reputation & trust</p>
              <TrustGauge agent={agent} />
            </div>
          </Reveal>

          {/* Invoke + Output — main working area */}
          <div className="agent-detail__work-grid">
            <Reveal delay={0.12}>
              <Card>
                <Card.Header>
                  <span className="agent-detail__section-title">Invoke</span>
                </Card.Header>
                <Card.Body>
                  {insufficientBalance && (
                    <div className="agent-detail__balance-warn">
                      <AlertTriangle size={15} color="var(--warn, #d97706)" style={{ flexShrink: 0 }} />
                      <div>
                        <p className="agent-detail__balance-warn-title">Insufficient balance</p>
                        <p className="agent-detail__balance-warn-sub">
                          Balance ${(balanceCents / 100).toFixed(2)} · Agent costs ${(priceCents / 100).toFixed(2)}
                        </p>
                        <Link to="/wallet">
                          <Button variant="primary" size="sm">Add funds →</Button>
                        </Link>
                      </div>
                    </div>
                  )}
                  <AgentInputForm
                    agent={agent}
                    mode={mode}
                    onModeChange={setMode}
                    onSubmit={handleInvoke}
                    loading={invokeLoading}
                  />
                </Card.Body>
              </Card>
            </Reveal>

            <Reveal delay={0.16}>
              <Card className="agent-detail__output-card">
                <Card.Header>
                  <span className="agent-detail__section-title">Output</span>
                </Card.Header>
                <Card.Body>
                  {jobInfo && (
                    <div className="agent-detail__job-queued">
                      <div className="agent-detail__job-queued-banner">
                        <p className="agent-detail__job-queued-label">Job queued</p>
                        <p className="agent-detail__job-id">{jobInfo.jobId}</p>
                      </div>
                      <div className="agent-detail__job-actions">
                        <Badge label={jobInfo.status} dot />
                        <Link to={`/jobs/${jobInfo.jobId}`}>
                          <Button variant="ghost" size="sm" iconRight={<ArrowUpRight size={13} />}>
                            View job
                          </Button>
                        </Link>
                      </div>
                    </div>
                  )}

                  {result && !jobInfo && (
                    <ResultRenderer result={result} agent={agent} />
                  )}

                  {!result && !jobInfo && (
                    <div className="agent-detail__output-empty">
                      <div className="agent-detail__output-empty-icon">
                        <BarChart2 size={28} strokeWidth={1.5} />
                      </div>
                      <p>Results appear here after invoking the agent.</p>
                    </div>
                  )}
                </Card.Body>
              </Card>
            </Reveal>
          </div>

          {/* Metrics detail */}
          <Reveal delay={0.18}>
            <Card>
              <Card.Header>
                <span className="agent-detail__section-title">Performance metrics</span>
              </Card.Header>
              <Card.Body>
                <div className="agent-detail__stats-grid">
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label" title="Trust score (0–100): weighted blend of success rate, dispute rate, call volume, quality ratings, and time-decay.">
                      Trust score ⓘ
                    </span>
                    <span className="agent-detail__stat-value">
                      {typeof agent.trust_score === 'number' ? agent.trust_score.toFixed(1) : '—'}
                    </span>
                  </div>
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label">Success rate</span>
                    <span className="agent-detail__stat-value">{fmtPct(agent.success_rate)}</span>
                  </div>
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label">Dispute rate</span>
                    <span className="agent-detail__stat-value">{fmtPct(agent.dispute_rate)}</span>
                  </div>
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label">Total calls</span>
                    <span className="agent-detail__stat-value">{agent.total_calls ?? '—'}</span>
                  </div>
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label">Avg latency</span>
                    <span className="agent-detail__stat-value">{fmtMs(agent.avg_latency_ms)}</span>
                  </div>
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label">Verified</span>
                    <span className="agent-detail__stat-value">{agent.verified ? 'Yes' : 'No'}</span>
                  </div>
                  {typeof agent.quality_rating_avg === 'number' && agent.quality_rating_count > 0 && (
                    <div className="agent-detail__stat">
                      <span className="agent-detail__stat-label">Quality</span>
                      <span className="agent-detail__stat-value agent-detail__stat-stars">
                        {'★'.repeat(Math.round(agent.quality_rating_avg))}
                        {'☆'.repeat(5 - Math.round(agent.quality_rating_avg))}
                        <span className="agent-detail__stat-rating-sub">
                          {' '}{agent.quality_rating_avg.toFixed(1)} ({agent.quality_rating_count})
                        </span>
                      </span>
                    </div>
                  )}
                </div>
              </Card.Body>
            </Card>
          </Reveal>

          {/* Work Portfolio */}
          {(workHistory !== null || workHistoryLoading) && (
            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <div className="agent-detail__portfolio-header">
                    <span className="agent-detail__section-title">
                      <BookOpen size={14} strokeWidth={2} />
                      Work portfolio
                    </span>
                    {workHistoryTotal > 0 && (
                      <span className="agent-detail__portfolio-count">{workHistoryTotal} example{workHistoryTotal !== 1 ? 's' : ''}</span>
                    )}
                  </div>
                </Card.Header>
                <Card.Body>
                  {workHistoryLoading && !workHistory && (
                    <div className="agent-detail__portfolio-loading">Loading work history…</div>
                  )}
                  {workHistory?.length === 0 && !workHistoryLoading && (
                    <div className="agent-detail__portfolio-empty">
                      No public work examples yet. Invoke this agent to generate examples.
                    </div>
                  )}
                  {(workHistory ?? []).length > 0 && (
                    <div className="agent-detail__portfolio-list">
                      {(workHistory ?? []).map((ex, i) => {
                        const key = ex.job_id ?? `${agent.agent_id}-ex-${i}`
                        const isExpanded = expandedExample === key
                        const rating = ex.rating ?? null
                        const qualityScore = ex.quality_score ?? null
                        const latency = ex.latency_ms != null ? `${Math.round(ex.latency_ms)}ms` : null
                        const ts = ex.created_at ? new Date(ex.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : null
                        return (
                          <div key={key} className={`agent-detail__portfolio-item${isExpanded ? ' agent-detail__portfolio-item--open' : ''}`}>
                            <button
                              className="agent-detail__portfolio-item-head"
                              onClick={() => setExpandedExample(isExpanded ? null : key)}
                              type="button"
                            >
                              <div className="agent-detail__portfolio-item-meta">
                                {ts && <span className="agent-detail__portfolio-ts">{ts}</span>}
                                {latency && <span className="agent-detail__portfolio-chip">{latency}</span>}
                                {qualityScore != null && (
                                  <span className="agent-detail__portfolio-chip agent-detail__portfolio-chip--quality">
                                    Q{qualityScore}/5
                                  </span>
                                )}
                                {rating != null && (
                                  <span className="agent-detail__portfolio-chip agent-detail__portfolio-chip--rating">
                                    {'★'.repeat(rating)}{'☆'.repeat(5 - rating)}
                                  </span>
                                )}
                                {ex.model_provider && (
                                  <ModelBadge provider={ex.model_provider} modelId={ex.model_id} size="xs" />
                                )}
                              </div>
                              <span className="agent-detail__portfolio-toggle">
                                {isExpanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                              </span>
                            </button>
                            <AnimatePresence>
                              {isExpanded && (
                                <motion.div
                                  className="agent-detail__portfolio-body"
                                  initial={{ height: 0, opacity: 0 }}
                                  animate={{ height: 'auto', opacity: 1 }}
                                  exit={{ height: 0, opacity: 0 }}
                                  transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
                                >
                                  {ex.input && (
                                    <div className="agent-detail__portfolio-block">
                                      <span className="agent-detail__portfolio-block-label">Input</span>
                                      <pre className="agent-detail__portfolio-pre">
                                        {JSON.stringify(ex.input, null, 2)}
                                      </pre>
                                    </div>
                                  )}
                                  {ex.output && (
                                    <div className="agent-detail__portfolio-block">
                                      <span className="agent-detail__portfolio-block-label">Output</span>
                                      <div className="agent-detail__portfolio-output">
                                        <ResultRenderer result={ex.output} agent={agent} />
                                      </div>
                                    </div>
                                  )}
                                  {Array.isArray(ex.artifacts) && ex.artifacts.length > 0 && (
                                    <div className="agent-detail__portfolio-block">
                                      <span className="agent-detail__portfolio-block-label">Artifacts</span>
                                      <div className="agent-detail__portfolio-artifacts">
                                        {ex.artifacts.map((a, ai) => (
                                          <a
                                            key={ai}
                                            className="agent-detail__portfolio-artifact"
                                            href={a.url_or_base64?.startsWith('http') ? a.url_or_base64 : undefined}
                                            target="_blank"
                                            rel="noopener noreferrer"
                                          >
                                            <span>{a.name ?? a.mime ?? 'artifact'}</span>
                                            {a.size_bytes != null && (
                                              <span className="agent-detail__portfolio-artifact-size">
                                                {(a.size_bytes / 1024).toFixed(1)}KB
                                              </span>
                                            )}
                                          </a>
                                        ))}
                                      </div>
                                    </div>
                                  )}
                                </motion.div>
                              )}
                            </AnimatePresence>
                          </div>
                        )
                      })}
                    </div>
                  )}
                  {workHistory && workHistory.length < workHistoryTotal && (
                    <button
                      className="agent-detail__portfolio-more"
                      onClick={() => loadWorkHistory(workHistoryOffset + 10)}
                      disabled={workHistoryLoading}
                      type="button"
                    >
                      {workHistoryLoading ? 'Loading…' : `Load more (${workHistoryTotal - workHistory.length} remaining)`}
                    </button>
                  )}
                </Card.Body>
              </Card>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
