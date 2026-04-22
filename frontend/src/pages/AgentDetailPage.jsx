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
import Stagger from '../ui/motion/Stagger'
import AgentSigil from '../brand/AgentSigil'
import { getAgentColor } from '../brand/sigilTraits'
import AgentInputForm from '../features/agents/AgentInputForm'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import TrustGauge from '../features/agents/TrustGauge'
import { callAgent, createJob, fetchAgentWorkHistory, fetchMyAgents } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowLeft, ArrowUpRight, AlertTriangle, Zap, Clock, BarChart2, Shield, ChevronDown, ChevronUp, BookOpen, Lock } from 'lucide-react'
import ModelBadge from '../components/ModelBadge'
import { BarChart, Bar, ResponsiveContainer, Tooltip as RechartTooltip } from 'recharts'
import './AgentDetailPage.css'

function healthDot(agent) {
  const status = agent.last_health_status
  const checkedAt = agent.last_health_check_at
  if (!status || status === 'unknown') return null
  const ageMs = checkedAt ? Date.now() - new Date(checkedAt).getTime() : Infinity
  const stale = ageMs > 10 * 60 * 1000
  let cls = 'ad__health-dot'
  let title = 'Health unknown'
  if (status === 'healthy' && !stale) {
    cls += ' ad__health-dot--healthy'
    title = `Healthy · checked ${new Date(checkedAt).toLocaleTimeString()}`
  } else if (status === 'unhealthy' || stale) {
    cls += ' ad__health-dot--warn'
    title = stale ? `Last check >10 min ago` : `Unhealthy · last checked ${new Date(checkedAt).toLocaleTimeString()}`
  }
  return <span className={cls} title={title} aria-label={title} />
}

function fmtPct(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function fmtMs(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${Math.round(value)} ms`
}

function relativeTime(isoString) {
  if (!isoString) return null
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 7) return `${days} day${days !== 1 ? 's' : ''} ago`
  const weeks = Math.floor(days / 7)
  return `${weeks} week${weeks !== 1 ? 's' : ''} ago`
}

function extractInputPreview(input) {
  if (!input || typeof input !== 'object') return String(input ?? '')
  for (const key of ['prompt', 'query', 'text', 'input']) {
    if (typeof input[key] === 'string' && input[key].length > 0) return input[key]
  }
  return JSON.stringify(input)
}

function extractOutputPreview(output) {
  if (!output || typeof output !== 'object') return String(output ?? '')
  for (const key of ['text', 'summary', 'result', 'answer']) {
    if (typeof output[key] === 'string' && output[key].length > 0) return output[key]
  }
  return JSON.stringify(output)
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
  const [expandedFields, setExpandedFields] = useState(new Set())

  const toggleField = (fieldKey) =>
    setExpandedFields(prev => {
      const next = new Set(prev)
      next.has(fieldKey) ? next.delete(fieldKey) : next.add(fieldKey)
      return next
    })

  const publicAgent = useMemo(() => agents.find(a => a.agent_id === id), [agents, id])
  const [ownerAgent, setOwnerAgent] = useState(null)
  const [ownerLookupDone, setOwnerLookupDone] = useState(false)

  useEffect(() => {
    if (publicAgent || !apiKey) {
      setOwnerLookupDone(!!publicAgent)
      return
    }
    let cancelled = false
    setOwnerLookupDone(false)
    fetchMyAgents(apiKey)
      .then(resp => {
        if (cancelled) return
        const list = resp?.agents || []
        setOwnerAgent(list.find(a => a.agent_id === id) || null)
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setOwnerLookupDone(true)
      })
    return () => { cancelled = true }
  }, [publicAgent, apiKey, id])

  const agent = publicAgent ?? ownerAgent

  const loadWorkHistory = useCallback(async (offset = 0) => {
    if (!agent || !apiKey) return
    setWorkHistoryLoading(true)
    try {
      const data = await fetchAgentWorkHistory(apiKey, agent.agent_id, { limit: 5, offset })
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

  const sparklineData = useMemo(() => {
    const days = 30
    const now = Date.now()
    const buckets = Array.from({ length: days }, (_, i) => {
      const d = new Date(now - (days - 1 - i) * 86400000)
      return { day: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }), jobs: 0 }
    })
    if (Array.isArray(workHistory)) {
      for (const item of workHistory) {
        const ts = item.completed_at || item.created_at
        if (!ts) continue
        const age = Math.floor((now - new Date(ts).getTime()) / 86400000)
        const idx = days - 1 - age
        if (idx >= 0 && idx < days) buckets[idx].jobs += 1
      }
    }
    return buckets
  }, [workHistory])

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
    if (!ownerLookupDone) {
      return (
        <main className="agent-detail">
          <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: 'Agent' }]} />
          <div className="agent-detail__scroll" />
        </main>
      )
    }
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
                    {healthDot(agent)}
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
                    {(agent.jobs_last_30_days > 0 || agent.job_completion_rate != null || agent.median_latency_seconds != null) && (
                      <div className="ad__reliability">
                        {agent.jobs_last_30_days > 0 && (
                          <span className="ad__stat-chip">{agent.jobs_last_30_days} jobs/30d</span>
                        )}
                        {agent.job_completion_rate != null && (
                          <span className="ad__stat-chip">{Math.round(agent.job_completion_rate * 100)}% completion</span>
                        )}
                        {agent.median_latency_seconds != null && (
                          <span className="ad__stat-chip">~{agent.median_latency_seconds}s median</span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </Reveal>

          {/* Activity sparkline */}
          {workHistory != null && (
            <Reveal delay={0.06}>
              <div className="ad__sparkline-card">
                <p className="ad__sparkline-title">Job volume · last 30 days</p>
                <ResponsiveContainer width="100%" height={48}>
                  <BarChart data={sparklineData} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
                    <Bar dataKey="jobs" fill="var(--accent)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
                    <RechartTooltip
                      cursor={false}
                      contentStyle={{ background: 'var(--surface-2)', border: '1px solid var(--border)', borderRadius: 6, fontSize: 11 }}
                      itemStyle={{ color: 'var(--text)' }}
                      formatter={(v, _, p) => [v, p.payload.day]}
                      labelFormatter={() => ''}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Reveal>
          )}

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

          {/* Recent Work */}
          {(workHistory !== null || workHistoryLoading) && (
            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <div className="agent-detail__portfolio-header">
                    <span className="agent-detail__section-title">
                      <BookOpen size={14} strokeWidth={2} />
                      Recent Work
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
                      No public work examples yet — be the first to hire this agent.
                    </div>
                  )}
                  {(workHistory ?? []).length > 0 && (
                    <Stagger className="agent-detail__portfolio-list" staggerDelay={0.07} delayStart={0.05}>
                      {(workHistory ?? []).map((ex, i) => {
                        const key = ex.job_id ?? `${agent.agent_id}-ex-${i}`
                        const isExpanded = expandedExample === key
                        const inputFieldKey = `${key}-input`
                        const outputFieldKey = `${key}-output`
                        const inputExpanded = expandedFields.has(inputFieldKey)
                        const outputExpanded = expandedFields.has(outputFieldKey)
                        const rating = ex.rating ?? null
                        const qualityScore = ex.quality_score ?? null
                        const latency = ex.latency_ms != null ? `${Math.round(ex.latency_ms)}ms` : null
                        const ts = relativeTime(ex.created_at)
                        const inputPreview = ex.input ? extractInputPreview(ex.input) : null
                        const outputPreview = ex.output ? extractOutputPreview(ex.output) : null
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
                                  {inputPreview != null && (
                                    <div className="agent-detail__portfolio-block">
                                      <span className="agent-detail__portfolio-block-label">Input</span>
                                      {inputExpanded ? (
                                        <>
                                          <pre className="agent-detail__portfolio-pre">
                                            {JSON.stringify(ex.input, null, 2)}
                                          </pre>
                                          <button className="agent-detail__portfolio-expand-link" onClick={() => toggleField(inputFieldKey)} type="button">
                                            collapse
                                          </button>
                                        </>
                                      ) : (
                                        <p className="agent-detail__portfolio-summary">
                                          {inputPreview.length > 200 ? inputPreview.slice(0, 200) + '…' : inputPreview}
                                          {inputPreview.length > 200 && (
                                            <button className="agent-detail__portfolio-expand-link" onClick={() => toggleField(inputFieldKey)} type="button">
                                              show more
                                            </button>
                                          )}
                                        </p>
                                      )}
                                    </div>
                                  )}
                                  {outputPreview != null && (
                                    <div className="agent-detail__portfolio-block">
                                      <span className="agent-detail__portfolio-block-label">Output</span>
                                      {outputExpanded ? (
                                        <>
                                          <div className="agent-detail__portfolio-output">
                                            <ResultRenderer result={ex.output} agent={agent} />
                                          </div>
                                          <button className="agent-detail__portfolio-expand-link" onClick={() => toggleField(outputFieldKey)} type="button">
                                            collapse
                                          </button>
                                        </>
                                      ) : (
                                        <p className="agent-detail__portfolio-summary">
                                          {outputPreview.length > 200 ? outputPreview.slice(0, 200) + '…' : outputPreview}
                                          {outputPreview.length > 200 && (
                                            <button className="agent-detail__portfolio-expand-link" onClick={() => toggleField(outputFieldKey)} type="button">
                                              show more
                                            </button>
                                          )}
                                        </p>
                                      )}
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
                    </Stagger>
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
