import { useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import EmptyState from '../ui/EmptyState'
import AgentInputForm from '../features/agents/AgentInputForm'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import { callAgent, createJob } from '../api'
import { useMarket } from '../context/MarketContext'

export default function AgentDetailPage() {
  const { id } = useParams()
  const { agents, apiKey, showToast, refreshJobs } = useMarket()
  const [mode, setMode] = useState('sync')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [meta, setMeta] = useState(null)

  const agent = useMemo(() => agents.find((item) => item.agent_id === id), [agents, id])

  const handleInvoke = async (payload) => {
    if (!agent) return
    setLoading(true)
    setMeta(null)
    try {
      if (mode === 'async') {
        const job = await createJob(apiKey, agent.agent_id, payload, 3)
        setResult(job)
        setMeta({ type: 'job', status: job.status, jobId: job.job_id })
        showToast?.(`Queued job ${job.job_id}`, 'success')
        await refreshJobs?.()
        return
      }
      const response = await callAgent(apiKey, agent.agent_id, payload)
      setResult(response.body)
      setMeta({ type: 'call', status: response.status })
      if (!response.ok) showToast?.(`Call failed with status ${response.status}`, 'error')
    } catch (err) {
      showToast?.(err?.message ?? 'Invoke failed', 'error')
    } finally {
      setLoading(false)
    }
  }

  if (!agent) {
    return (
      <main style={{ padding: 24, display: 'grid', gap: 16 }}>
        <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: 'Agent detail' }]} />
        <EmptyState
          title="Agent not found"
          sub="This agent may have been removed."
          action={<Link to="/agents"><Button variant="secondary">Back to agents</Button></Link>}
        />
      </main>
    )
  }

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ to: '/agents', label: 'Agents' }, { label: agent.name }]} />
      <section style={{ display: 'grid', gap: 12, gridTemplateColumns: 'minmax(280px, 420px) 1fr' }}>
        <AgentInputForm
          agent={agent}
          mode={mode}
          onModeChange={setMode}
          onSubmit={handleInvoke}
          loading={loading}
        />
        <Card>
          <Card.Header>
            <strong>Output</strong>
          </Card.Header>
          <Card.Body>
            {meta && (
              <p style={{ color: 'var(--ink-mute)', marginBottom: 12 }}>
                {meta.type === 'job' ? `Job ${meta.jobId} · ${meta.status}` : `HTTP status ${meta.status}`}
              </p>
            )}
            <ResultRenderer result={result} agent={agent} />
            {!result && <p style={{ color: 'var(--ink-mute)' }}>Run the agent to see output here.</p>}
          </Card.Body>
        </Card>
      </section>
    </main>
  )
}
