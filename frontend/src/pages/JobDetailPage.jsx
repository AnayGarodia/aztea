import { useEffect, useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import EmptyState from '../ui/EmptyState'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import { getJobMessages } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowLeft, RefreshCw } from 'lucide-react'

function fmtDate(str) {
  if (!str) return '--'
  return new Date(str).toLocaleString(undefined, {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

function fmtUsd(cents) {
  if (typeof cents !== 'number') return null
  return '$' + (cents / 100).toFixed(2)
}

function InfoRow({ label, value, mono = false }) {
  return (
    <div style={{ display: 'flex', gap: 'var(--sp-4)', alignItems: 'flex-start', padding: '9px 0', borderBottom: '1px solid var(--line)' }}>
      <span style={{
        minWidth: 140, fontSize: '0.8125rem', fontWeight: 500,
        color: 'var(--ink-mute)', flexShrink: 0,
      }}>
        {label}
      </span>
      <span style={{
        fontSize: '0.875rem', color: 'var(--ink)',
        fontFamily: mono ? 'var(--font-mono)' : undefined,
        fontFeatureSettings: mono ? '"tnum"' : undefined,
        wordBreak: 'break-all',
      }}>
        {value}
      </span>
    </div>
  )
}

function MessageBubble({ msg }) {
  const isSystem = msg.from_id?.startsWith('system') || msg.type?.startsWith('claim')
  const isWorker = msg.from_id && !isSystem

  return (
    <div style={{
      padding: 'var(--sp-4)',
      background: isSystem ? 'var(--canvas-sunk)' : 'var(--surface)',
      border: '1px solid',
      borderColor: isSystem ? 'var(--line)' : 'var(--line-strong)',
      borderRadius: 'var(--r-md)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)', marginBottom: 'var(--sp-2)' }}>
        <Badge label={msg.type ?? 'message'} />
        {msg.from_id && (
          <span style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', fontFamily: 'var(--font-mono)' }}>
            {msg.from_id}
          </span>
        )}
        {msg.created_at && (
          <span style={{ fontSize: '0.75rem', color: 'var(--ink-faint)', marginLeft: 'auto' }}>
            {fmtDate(msg.created_at)}
          </span>
        )}
      </div>
      {msg.payload && (
        <pre style={{
          margin: 0, fontSize: '0.8125rem', color: 'var(--ink-soft)',
          fontFamily: 'var(--font-mono)', whiteSpace: 'pre-wrap', lineHeight: 1.6,
        }}>
          {typeof msg.payload === 'string' ? msg.payload : JSON.stringify(msg.payload, null, 2)}
        </pre>
      )}
    </div>
  )
}

export default function JobDetailPage() {
  const { id } = useParams()
  const { jobs, agents, apiKey, refreshJobs } = useMarket()
  const [messages, setMessages] = useState([])
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  const job = useMemo(() => jobs.find(j => j.job_id === id), [jobs, id])
  const agent = useMemo(() => agents.find(a => a.agent_id === job?.agent_id), [agents, job])

  const loadMessages = async () => {
    if (!id || !apiKey) return
    setLoadingMsgs(true)
    try {
      const res = await getJobMessages(apiKey, id)
      setMessages(Array.isArray(res?.messages) ? res.messages : [])
    } catch {
      setMessages([])
    } finally {
      setLoadingMsgs(false)
    }
  }

  useEffect(() => { loadMessages() }, [apiKey, id]) // eslint-disable-line

  const handleRefresh = async () => {
    setRefreshing(true)
    await Promise.all([refreshJobs?.(), loadMessages()])
    setRefreshing(false)
  }

  if (!job) {
    return (
      <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
        <Topbar crumbs={[{ to: '/jobs', label: 'Jobs' }, { label: 'Job' }]} />
        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>
          <EmptyState
            title="Job not found"
            sub="This job may not be visible to your key."
            action={
              <Link to="/jobs">
                <Button variant="secondary" icon={<ArrowLeft size={14} />}>Back to jobs</Button>
              </Link>
            }
          />
        </div>
      </main>
    )
  }

  const isTerminal = job.status === 'complete' || job.status === 'failed'
  const output = job.output_payload

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ to: '/jobs', label: 'Jobs' }, { label: job.job_id.slice(0, 12) + '…' }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Job header */}
        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 'var(--sp-4)', marginBottom: 'var(--sp-6)', flexWrap: 'wrap' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)', marginBottom: 8 }}>
              <Badge label={job.status} dot />
              {agent && (
                <Link to={`/agents/${agent.agent_id}`} style={{ fontSize: '0.875rem', color: 'var(--accent)', textDecoration: 'none', fontWeight: 500 }}>
                  {agent.name}
                </Link>
              )}
            </div>
            <p style={{ fontFamily: 'var(--font-mono)', fontSize: '0.875rem', color: 'var(--ink-mute)' }}>
              {job.job_id}
            </p>
          </div>
          {!isTerminal && (
            <Button
              variant="secondary"
              size="sm"
              icon={<RefreshCw size={13} />}
              onClick={handleRefresh}
              loading={refreshing}
            >
              Refresh
            </Button>
          )}
        </div>

        <div style={{ display: 'grid', gap: 'var(--sp-5)' }}>

          {/* Job metadata */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Details</span>
            </Card.Header>
            <Card.Body>
              <InfoRow label="Status" value={<Badge label={job.status} dot />} />
              {fmtUsd(job.price_cents) && <InfoRow label="Cost" value={fmtUsd(job.price_cents)} mono />}
              {job.attempt_count != null && <InfoRow label="Attempts" value={`${job.attempt_count} / ${job.max_attempts ?? '—'}`} mono />}
              <InfoRow label="Created" value={fmtDate(job.created_at)} />
              {job.completed_at && <InfoRow label="Completed" value={fmtDate(job.completed_at)} />}
              {job.error_message && (
                <div style={{ marginTop: 'var(--sp-3)', padding: 'var(--sp-4)', background: 'var(--negative-wash)', border: '1px solid var(--negative-line)', borderRadius: 'var(--r-md)' }}>
                  <p style={{ fontSize: '0.8125rem', fontWeight: 600, color: 'var(--negative)', marginBottom: 4 }}>Error</p>
                  <p style={{ fontSize: '0.875rem', color: 'var(--negative)', lineHeight: 1.5 }}>{job.error_message}</p>
                </div>
              )}
            </Card.Body>
          </Card>

          {/* Input payload */}
          {job.input_payload && (
            <Card>
              <Card.Header>
                <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Input</span>
              </Card.Header>
              <Card.Body>
                <pre style={{
                  margin: 0, fontFamily: 'var(--font-mono)', fontSize: '0.8125rem',
                  color: 'var(--ink-soft)', whiteSpace: 'pre-wrap', lineHeight: 1.7,
                  background: 'var(--canvas-sunk)', border: '1px solid var(--line)',
                  borderRadius: 'var(--r-sm)', padding: 'var(--sp-4)',
                }}>
                  {JSON.stringify(job.input_payload, null, 2)}
                </pre>
              </Card.Body>
            </Card>
          )}

          {/* Output */}
          {output && (
            <Card>
              <Card.Header>
                <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Output</span>
              </Card.Header>
              <Card.Body>
                {agent ? (
                  <ResultRenderer result={output} agent={agent} />
                ) : (
                  <pre style={{
                    margin: 0, fontFamily: 'var(--font-mono)', fontSize: '0.8125rem',
                    color: 'var(--ink-soft)', whiteSpace: 'pre-wrap', lineHeight: 1.7,
                    background: 'var(--canvas-sunk)', border: '1px solid var(--line)',
                    borderRadius: 'var(--r-sm)', padding: 'var(--sp-4)', maxHeight: 480, overflow: 'auto',
                  }}>
                    {JSON.stringify(output, null, 2)}
                  </pre>
                )}
              </Card.Body>
            </Card>
          )}

          {/* Messages */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>
                Messages {messages.length > 0 && `(${messages.length})`}
              </span>
            </Card.Header>
            <Card.Body>
              {loadingMsgs ? (
                <p style={{ color: 'var(--ink-mute)', fontSize: '0.875rem' }}>Loading…</p>
              ) : messages.length === 0 ? (
                <p style={{ color: 'var(--ink-mute)', fontSize: '0.875rem' }}>No messages on this job.</p>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-3)' }}>
                  {messages.map(msg => (
                    <MessageBubble key={msg.message_id} msg={msg} />
                  ))}
                </div>
              )}
            </Card.Body>
          </Card>

        </div>
      </div>
    </main>
  )
}
