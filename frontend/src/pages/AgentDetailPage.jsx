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
import AgentInputForm from '../features/agents/AgentInputForm'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import TrustGauge from '../features/agents/TrustGauge'
import { callAgent, createJob } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowLeft, ArrowUpRight, AlertTriangle } from 'lucide-react'
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
  const priceCents = agent ? Math.round((agent.price_per_call_usd ?? 0) * 100) : 0
  const balanceCents = wallet?.balance_cents ?? 0
  const insufficientBalance = priceCents > 0 && balanceCents < priceCents
  const outputExamples = Array.isArray(agent?.output_examples) ? agent.output_examples : []

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
            <div className="agent-detail__hero">
              <div className="agent-detail__hero-row">
                <div className="agent-detail__hero-left">
                  <AgentSigil agentId={agent.agent_id} size="lg" state="idle" />
                  <div className="agent-detail__hero-meta">
                    <span className="agent-detail__eyebrow">Agent profile</span>
                    <h1 className="agent-detail__name">{agent.name}</h1>
                    {agent.description && (
                      <p className="agent-detail__description">{agent.description}</p>
                    )}
                    {(agent.tags ?? []).length > 0 && (
                      <div className="agent-detail__tags">
                        {agent.tags.map(t => <Pill key={t} size="sm">{t}</Pill>)}
                        {agent.model_provider && (
                          <ModelBadge provider={agent.model_provider} modelId={agent.model_id} />
                        )}
                      </div>
                    )}
                  </div>
                </div>
                <div className="agent-detail__price-col">
                  <span className="agent-detail__price">
                    ${Number(agent.price_per_call_usd).toFixed(2)}
                  </span>
                  <span className="agent-detail__price-label">per call</span>
                </div>
              </div>
            </div>
          </Reveal>

          {/* Trust gauge */}
          <Reveal delay={0.1}>
            <motion.div
              className="agent-detail__trust"
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.15, duration: 0.4 }}
            >
              <p className="agent-detail__trust-title">Reputation & trust</p>
              <TrustGauge agent={agent} />
            </motion.div>
          </Reveal>

          <Reveal delay={0.12}>
            <Card>
              <Card.Header>
                <span className="agent-detail__section-title">Public profile</span>
              </Card.Header>
              <Card.Body>
                <div className="agent-detail__stats-grid">
                  <div className="agent-detail__stat">
                    <span className="agent-detail__stat-label">Trust score</span>
                    <span className="agent-detail__stat-value">
                      {typeof agent.trust_score === 'number' ? agent.trust_score.toFixed(2) : '—'}
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
                </div>
              </Card.Body>
            </Card>
          </Reveal>

          <Reveal delay={0.14}>
            <Card>
              <Card.Header>
                <span className="agent-detail__section-title">Output examples</span>
              </Card.Header>
              <Card.Body>
                {outputExamples.length === 0 ? (
                  <p className="agent-detail__output-empty">
                    No examples provided yet.
                  </p>
                ) : (
                  <div className="agent-detail__examples">
                    {outputExamples.map((example, index) => (
                      <div key={`${agent.agent_id}-example-${index}`} className="agent-detail__example">
                        <p className="agent-detail__example-title">Example {index + 1}</p>
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
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {/* Invocation guide */}
          <Reveal delay={0.15}>
            <div className="agent-detail__guide">
              <p className="agent-detail__guide-title">Invocation guide</p>
              <ul>
                <li>Use <strong>Sync</strong> for immediate output on this page.</li>
                <li>Use <strong>Async</strong> to queue long jobs and monitor them in Jobs.</li>
                <li>Price is charged before run and refunded on failed execution.</li>
              </ul>
            </div>
          </Reveal>

          {/* Two-column: invoke + output */}
          <div className="agent-detail__invoke-grid">
            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <span className="agent-detail__section-title">Invoke</span>
                </Card.Header>
                <Card.Body>
                  {/* Low-balance warning */}
                  {insufficientBalance && (
                    <div style={{
                      display: 'flex', alignItems: 'flex-start', gap: 'var(--sp-3)',
                      padding: 'var(--sp-3) var(--sp-4)',
                      background: 'var(--warn-wash, #fffbe6)',
                      border: '1px solid var(--warn-line, #f0d060)',
                      borderRadius: 'var(--r-md)',
                      marginBottom: 'var(--sp-4)',
                    }}>
                      <AlertTriangle size={16} color="var(--warn, #d97706)" style={{ flexShrink: 0, marginTop: 2 }} />
                      <div>
                        <p style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--ink)', marginBottom: 2 }}>
                          Insufficient balance
                        </p>
                        <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', marginBottom: 'var(--sp-2)' }}>
                          Your balance (${(balanceCents / 100).toFixed(2)}) is less than this agent's price (${(priceCents / 100).toFixed(2)}).
                          The call will be rejected until you add funds.
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

            <Reveal delay={0.25}>
              <Card>
                <Card.Header>
                  <span className="agent-detail__section-title">Output</span>
                </Card.Header>
                <Card.Body>
                  {jobInfo && (
                    <div className="agent-detail__job-queued">
                      <div className="agent-detail__job-queued-banner">
                        <p className="agent-detail__job-queued-label">Job queued successfully</p>
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
                    <p className="agent-detail__output-empty">
                      Submit payload to run the agent. Sync results render here; async jobs open in Jobs.
                    </p>
                  )}
                </Card.Body>
              </Card>
            </Reveal>
          </div>

        </div>
      </div>
    </main>
  )
}
