import { useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { motion } from 'motion/react'
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
import { callAgent, createJob } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowLeft, ArrowUpRight, AlertTriangle, Zap, Clock, BarChart2, Shield } from 'lucide-react'
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

  const agent = useMemo(() => agents.find(a => a.agent_id === id), [agents, id])
  const accentColor = agent ? getAgentColor(agent.agent_id) : null
  const priceCents = agent ? Math.round((agent.price_per_call_usd ?? 0) * 100) : 0
  const balanceCents = wallet?.balance_cents ?? 0
  const insufficientBalance = priceCents > 0 && balanceCents < priceCents
  const outputExamples = Array.isArray(agent?.output_examples) ? agent.output_examples : []
  const trustScore = typeof agent?.trust_score === 'number' ? Math.round(agent.trust_score) : null
  const successPct = agent?.success_rate != null ? Math.round(agent.success_rate * 100) : null
  const highDispute = typeof agent?.dispute_rate === 'number' && agent.dispute_rate > 0.10

  const handleInvoke = async (payload) => {
    if (!agent) return
    setInvokeLoading(true)
    setResult(null)
    setJobInfo(null)
    try {
      if (mode === 'async') {
        const job = await createJob(apiKey, agent.agent_id, payload, 3)
        setJobInfo({ jobId: job.job_id, status: job.status })
        showToast?.(`Job queued — ${job.job_id.slice(0, 8)}`, 'success')
        await refreshJobs?.()
        return
      }
      const response = await callAgent(apiKey, agent.agent_id, payload)
      setResult(response.body)
      if (!response.ok) showToast?.(`Call failed (${response.status})`, 'error')
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

          {/* Output examples */}
          {outputExamples.length > 0 && (
            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <span className="agent-detail__section-title">Output examples</span>
                </Card.Header>
                <Card.Body>
                  <div className="agent-detail__examples">
                    {outputExamples.map((example, i) => (
                      <div key={`${agent.agent_id}-example-${i}`} className="agent-detail__example">
                        <p className="agent-detail__example-title">Example {i + 1}</p>
                        <div className="agent-detail__example-block">
                          <span>Input</span>
                          <pre>{JSON.stringify(example?.input ?? {}, null, 2)}</pre>
                        </div>
                        <div className="agent-detail__example-block">
                          <span>Output</span>
                          <pre>{JSON.stringify(example?.output ?? {}, null, 2)}</pre>
                        </div>
                      </div>
                    ))}
                  </div>
                </Card.Body>
              </Card>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
