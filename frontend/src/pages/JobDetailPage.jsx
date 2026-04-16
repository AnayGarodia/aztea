import { useEffect, useMemo, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import AgentSigil from '../brand/AgentSigil'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import { getJobMessages } from '../api'
import { useMarket } from '../context/MarketContext'
import JobTimeline from '../features/jobs/JobTimeline'
import { ArrowLeft, RefreshCw } from 'lucide-react'
import './JobDetailPage.css'

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
    <div className="job-detail__info-row">
      <span className="job-detail__info-label">{label}</span>
      <span className={`job-detail__info-value${mono ? ' job-detail__info-value--mono' : ''}`}>
        {value}
      </span>
    </div>
  )
}

function MessageBubble({ msg }) {
  const isSystem = msg.from_id?.startsWith('system') || msg.type?.startsWith('claim')

  return (
    <div className={`job-detail__msg${!isSystem ? ' job-detail__msg--highlight' : ''}`}>
      <div className="job-detail__msg-meta">
        <Badge label={msg.type ?? 'message'} />
        {msg.from_id && (
          <span className="job-detail__msg-from">{msg.from_id}</span>
        )}
        {msg.created_at && (
          <span className="job-detail__msg-time">{fmtDate(msg.created_at)}</span>
        )}
      </div>
      {msg.payload && (
        <pre className="job-detail__msg-payload">
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
      <main className="job-detail">
        <Topbar crumbs={[{ to: '/jobs', label: 'Jobs' }, { label: 'Job' }]} />
        <div className="job-detail__scroll">
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
    <main className="job-detail">
      <Topbar crumbs={[{ to: '/jobs', label: 'Jobs' }, { label: job.job_id.slice(0, 12) + '…' }]} />

      <div className="job-detail__scroll">
        <div className="job-detail__content">

          {/* Header */}
          <Reveal>
            <div className="job-detail__header">
              <div className="job-detail__header-left">
                <div className="job-detail__header-row">
                  <Badge label={job.status} dot />
                  {agent && (
                    <Link to={`/agents/${agent.agent_id}`} className="job-detail__agent-link">
                      <AgentSigil agentId={agent.agent_id} size="xs" />
                      {agent.name}
                    </Link>
                  )}
                </div>
                <p className="job-detail__id">{job.job_id}</p>
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
          </Reveal>

          {/* Timeline */}
          <Reveal delay={0.05}>
            <div className="job-detail__timeline">
              <p className="job-detail__timeline-title">Progress</p>
              <JobTimeline status={job.status} />
            </div>
          </Reveal>

          {/* Job metadata */}
          <Reveal delay={0.1}>
            <Card>
              <Card.Header>
                <span className="job-detail__section-title">Details</span>
              </Card.Header>
              <Card.Body>
                <InfoRow label="Status" value={<Badge label={job.status} dot />} />
                {fmtUsd(job.price_cents) && <InfoRow label="Cost" value={fmtUsd(job.price_cents)} mono />}
                {job.attempt_count != null && (
                  <InfoRow label="Attempts" value={`${job.attempt_count} / ${job.max_attempts ?? '—'}`} mono />
                )}
                <InfoRow label="Created" value={fmtDate(job.created_at)} />
                {job.completed_at && <InfoRow label="Completed" value={fmtDate(job.completed_at)} />}
                {job.error_message && (
                  <div className="job-detail__error-box">
                    <p className="job-detail__error-title">Error</p>
                    <p className="job-detail__error-msg">{job.error_message}</p>
                  </div>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {/* Input payload */}
          {job.input_payload && (
            <Reveal delay={0.15}>
              <Card>
                <Card.Header>
                  <span className="job-detail__section-title">Input</span>
                </Card.Header>
                <Card.Body>
                  <pre className="job-detail__json">
                    {JSON.stringify(job.input_payload, null, 2)}
                  </pre>
                </Card.Body>
              </Card>
            </Reveal>
          )}

          {/* Output */}
          {output && (
            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <span className="job-detail__section-title">Output</span>
                </Card.Header>
                <Card.Body>
                  {agent ? (
                    <ResultRenderer result={output} agent={agent} />
                  ) : (
                    <pre className="job-detail__json">
                      {JSON.stringify(output, null, 2)}
                    </pre>
                  )}
                </Card.Body>
              </Card>
            </Reveal>
          )}

          {/* Messages */}
          <Reveal delay={0.25}>
            <Card>
              <Card.Header>
                <span className="job-detail__section-title">
                  Messages {messages.length > 0 && `(${messages.length})`}
                </span>
              </Card.Header>
              <Card.Body>
                {loadingMsgs ? (
                  <p className="job-detail__no-msg">Loading…</p>
                ) : messages.length === 0 ? (
                  <p className="job-detail__no-msg">No messages on this job.</p>
                ) : (
                  <div className="job-detail__messages">
                    {messages.map(msg => (
                      <MessageBubble key={msg.message_id} msg={msg} />
                    ))}
                  </div>
                )}
              </Card.Body>
            </Card>
          </Reveal>

        </div>
      </div>
    </main>
  )
}
