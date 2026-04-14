import { useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Pill from '../ui/Pill'
import EmptyState from '../ui/EmptyState'
import AgentInputForm from '../features/agents/AgentInputForm'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import { callAgent, createJob } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowLeft, ArrowUpRight } from 'lucide-react'

function StatChip({ label, value }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 80 }}>
      <span style={{
        fontFamily: 'var(--font-mono)', fontSize: '0.9375rem',
        fontWeight: 500, color: 'var(--ink)', fontFeatureSettings: '"tnum"',
      }}>
        {value}
      </span>
      <span style={{
        fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.05em',
        textTransform: 'uppercase', color: 'var(--ink-mute)',
      }}>
        {label}
      </span>
    </div>
  )
}

export default function AgentDetailPage() {
  const { id } = useParams()
  const { agents, apiKey, showToast, refreshJobs } = useMarket()
  const [mode, setMode] = useState('sync')
  const [invokeLoading, setInvokeLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [jobInfo, setJobInfo] = useState(null)

  const agent = useMemo(() => agents.find(a => a.agent_id === id), [agents, id])

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
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
        <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: 'Agent' }]} />
        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>
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

  const successPct = agent.success_rate != null ? `${Math.round(agent.success_rate * 100)}%` : '—'
  const latency = agent.avg_latency_ms != null ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—'
  const calls = agent.total_calls ?? 0

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: agent.name }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Agent header */}
        <div style={{ marginBottom: 'var(--sp-6)' }}>
          <div style={{
            display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
            gap: 'var(--sp-5)', flexWrap: 'wrap', marginBottom: 'var(--sp-4)',
          }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <h1 style={{
                fontFamily: 'var(--font-display)',
                fontSize: '1.75rem', fontWeight: 400,
                color: 'var(--ink)', letterSpacing: '-0.02em', lineHeight: 1.15,
                marginBottom: 8,
              }}>
                {agent.name}
              </h1>
              {agent.description && (
                <p style={{ fontSize: '0.9375rem', color: 'var(--ink-soft)', lineHeight: 1.6, maxWidth: 600, marginBottom: 'var(--sp-3)' }}>
                  {agent.description}
                </p>
              )}
              {(agent.tags ?? []).length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--sp-1)' }}>
                  {agent.tags.map(t => <Pill key={t} size="sm">{t}</Pill>)}
                </div>
              )}
            </div>
            <div style={{
              display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4, flexShrink: 0,
            }}>
              <span style={{
                fontFamily: 'var(--font-mono)', fontSize: '1.5rem',
                fontWeight: 500, color: 'var(--accent)', fontFeatureSettings: '"tnum"', lineHeight: 1,
              }}>
                ${Number(agent.price_per_call_usd).toFixed(2)}
              </span>
              <span style={{ fontSize: '0.75rem', color: 'var(--ink-mute)' }}>per call</span>
            </div>
          </div>

          {/* Stats */}
          <div style={{
            display: 'flex', gap: 'var(--sp-6)', padding: 'var(--sp-4) var(--sp-5)',
            background: 'var(--canvas-sunk)', border: '1px solid var(--line)',
            borderRadius: 'var(--r-md)', flexWrap: 'wrap',
          }}>
            <StatChip label="Success rate" value={successPct} />
            <StatChip label="Avg latency" value={latency} />
            <StatChip label="Total calls" value={calls.toLocaleString()} />
          </div>
        </div>

        {/* Two-column: invoke + output */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(280px, 360px) 1fr',
          gap: 'var(--sp-5)',
          alignItems: 'start',
        }}>
          {/* Invoke panel */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Invoke</span>
            </Card.Header>
            <Card.Body>
              <AgentInputForm
                agent={agent}
                mode={mode}
                onModeChange={setMode}
                onSubmit={handleInvoke}
                loading={invokeLoading}
              />
            </Card.Body>
          </Card>

          {/* Output panel */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Output</span>
            </Card.Header>
            <Card.Body>
              {/* Async job created */}
              {jobInfo && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-3)' }}>
                  <div style={{
                    padding: 'var(--sp-4)', background: 'var(--accent-wash)',
                    border: '1px solid var(--accent-line)', borderRadius: 'var(--r-md)',
                  }}>
                    <p style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--accent-ink)', marginBottom: 4 }}>
                      Job queued successfully
                    </p>
                    <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', fontFamily: 'var(--font-mono)' }}>
                      {jobInfo.jobId}
                    </p>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)' }}>
                    <Badge label={jobInfo.status} dot />
                    <Link to={`/jobs/${jobInfo.jobId}`}>
                      <Button variant="ghost" size="sm" iconRight={<ArrowUpRight size={13} />}>
                        View job
                      </Button>
                    </Link>
                  </div>
                </div>
              )}

              {/* Sync result */}
              {result && !jobInfo && (
                <ResultRenderer result={result} agent={agent} />
              )}

              {/* Empty state */}
              {!result && !jobInfo && (
                <p style={{ color: 'var(--ink-mute)', fontSize: '0.875rem' }}>
                  Run the agent to see output here.
                </p>
              )}
            </Card.Body>
          </Card>
        </div>
      </div>
    </main>
  )
}
