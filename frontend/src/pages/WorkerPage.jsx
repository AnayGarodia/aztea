import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Topbar from '../layout/Topbar'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Textarea from '../ui/Textarea'
import Card from '../ui/Card'
import Reveal from '../ui/motion/Reveal'
import {
  claimJob,
  completeJob,
  failJob,
  fetchAgentJobs,
  fetchAllAgentJobs,
  getJobMessages,
  heartbeatJob,
  postJobMessage,
} from '../api'
import { useMarket } from '../context/MarketContext'
import './WorkerPage.css'

const OPEN_STATUSES = new Set(['pending', 'running', 'awaiting_clarification'])

function fmtDate(str) {
  if (!str) return '--'
  return new Date(str).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function fmtLeaseRemaining(leaseExpiresAt, nowMs) {
  if (!leaseExpiresAt) return '--'
  const target = Date.parse(leaseExpiresAt)
  if (!Number.isFinite(target)) return '--'
  const ms = target - nowMs
  if (ms <= 0) return 'Expired'
  const total = Math.floor(ms / 1000)
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}

function parseJsonObject(raw, label) {
  const text = String(raw ?? '').trim()
  if (!text) return {}
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error(`${label} must be valid JSON.`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object.`)
  }
  return parsed
}

export default function WorkerPage() {
  const { agents, loading, apiKey, showToast } = useMarket()
  const [ownedAgents, setOwnedAgents] = useState([])
  const [workerJobs, setWorkerJobs] = useState([])
  const [loadingJobs, setLoadingJobs] = useState(true)
  const [discovering, setDiscovering] = useState(false)
  const [actionLoading, setActionLoading] = useState({})
  const [actionErrors, setActionErrors] = useState({})
  const [refreshingNow, setRefreshingNow] = useState(false)
  const [outputDrafts, setOutputDrafts] = useState({})
  const [failDrafts, setFailDrafts] = useState({})
  const [messageDrafts, setMessageDrafts] = useState({})
  const [threadOpen, setThreadOpen] = useState({})
  const [threadLoading, setThreadLoading] = useState({})
  const [messagesByJob, setMessagesByJob] = useState({})
  const [tickNow, setTickNow] = useState(Date.now())
  const pollingRef = useRef(false)
  const agentIdsKey = useMemo(
    () => agents.map(agent => agent.agent_id).sort().join('|'),
    [agents],
  )

  const setLoadingFor = (jobId, action, value) => {
    const key = `${jobId}:${action}`
    setActionLoading(prev => ({ ...prev, [key]: value }))
  }
  const isLoadingFor = (jobId, action) => Boolean(actionLoading[`${jobId}:${action}`])
  const setErrorFor = (jobId, msg) => setActionErrors(prev => ({ ...prev, [jobId]: msg }))
  const clearErrorFor = (jobId) => setActionErrors(prev => { const n = { ...prev }; delete n[jobId]; return n })

  const loadMessagesForJob = useCallback(async (jobId) => {
    setThreadLoading(prev => ({ ...prev, [jobId]: true }))
    try {
      const data = await getJobMessages(apiKey, jobId)
      setMessagesByJob(prev => ({ ...prev, [jobId]: Array.isArray(data?.messages) ? data.messages : [] }))
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to load messages.', 'error')
    } finally {
      setThreadLoading(prev => ({ ...prev, [jobId]: false }))
    }
  }, [apiKey, showToast])

  useEffect(() => {
    const interval = setInterval(() => setTickNow(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!apiKey || agents.length === 0) {
      setOwnedAgents([])
      setDiscovering(false)
      return
    }

    const discover = async () => {
      setDiscovering(true)
      const checks = await Promise.allSettled(
        agents.map(async agent => {
          await fetchAgentJobs(apiKey, agent.agent_id, { limit: 1 })
          return { agent }
        }),
      )
      if (cancelled) return
      const mine = []
      for (const result of checks) {
        if (result.status === 'fulfilled') {
          mine.push(result.value.agent)
          continue
        }
        const status = result.reason?.status
        if (status === 403 || status === 404) continue
      }
      setOwnedAgents(mine)
      setDiscovering(false)
    }

    discover().catch(err => {
      if (cancelled) return
      setDiscovering(false)
      showToast?.(err?.message ?? 'Failed to discover worker agents.', 'error')
    })
    return () => { cancelled = true }
  }, [agents, agentIdsKey, apiKey, showToast])

  const refreshWorkerJobs = useCallback(async () => {
    if (!apiKey || ownedAgents.length === 0) {
      setWorkerJobs([])
      setLoadingJobs(false)
      return
    }
    if (pollingRef.current) return
    pollingRef.current = true
    try {
      const batches = await Promise.all(
        ownedAgents.map(agent => fetchAllAgentJobs(apiKey, agent.agent_id, { pageSize: 100, maxPages: 5 })),
      )
      const byAgentId = new Map(ownedAgents.map(agent => [agent.agent_id, agent]))
      const merged = []
      batches.forEach(batch => {
        ;(batch.jobs ?? []).forEach(job => {
          if (!OPEN_STATUSES.has(job.status)) return
          const agent = byAgentId.get(job.agent_id)
          merged.push({
            ...job,
            _agent_name: agent?.name ?? job.agent_id,
          })
        })
      })
      merged.sort((a, b) => Date.parse(b.created_at || 0) - Date.parse(a.created_at || 0))
      setWorkerJobs(merged)
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to refresh worker jobs.', 'error')
    } finally {
      pollingRef.current = false
      setLoadingJobs(false)
    }
  }, [apiKey, ownedAgents, showToast])

  useEffect(() => {
    setLoadingJobs(true)
    refreshWorkerJobs()
  }, [refreshWorkerJobs])

  useEffect(() => {
    if (!apiKey) return undefined
    const interval = setInterval(() => {
      refreshWorkerJobs()
      Object.keys(threadOpen).forEach(jobId => {
        if (threadOpen[jobId]) loadMessagesForJob(jobId)
      })
    }, 10000)
    return () => clearInterval(interval)
  }, [apiKey, refreshWorkerJobs, threadOpen, loadMessagesForJob])

  const ownedAgentNames = useMemo(
    () => ownedAgents.map(agent => agent.name).sort((a, b) => a.localeCompare(b)),
    [ownedAgents],
  )

  const handleClaim = async (job) => {
    clearErrorFor(job.job_id)
    setLoadingFor(job.job_id, 'claim', true)
    try {
      await claimJob(apiKey, job.job_id, 300)
      showToast?.('Job claimed.', 'success')
      await refreshWorkerJobs()
    } catch (err) {
      setErrorFor(job.job_id, err?.message ?? 'Claim failed.')
    } finally {
      setLoadingFor(job.job_id, 'claim', false)
    }
  }

  const handleHeartbeat = async (job) => {
    clearErrorFor(job.job_id)
    setLoadingFor(job.job_id, 'heartbeat', true)
    try {
      await heartbeatJob(apiKey, job.job_id, 300, job.claim_token)
      showToast?.('Lease extended.', 'success')
      await refreshWorkerJobs()
    } catch (err) {
      setErrorFor(job.job_id, err?.message ?? 'Heartbeat failed.')
    } finally {
      setLoadingFor(job.job_id, 'heartbeat', false)
    }
  }

  const handleComplete = async (job) => {
    clearErrorFor(job.job_id)
    let parsedOutput
    try {
      parsedOutput = parseJsonObject(outputDrafts[job.job_id] ?? '{}', 'Output payload')
    } catch (err) {
      setErrorFor(job.job_id, err.message)
      return
    }
    setLoadingFor(job.job_id, 'complete', true)
    try {
      await completeJob(apiKey, job.job_id, parsedOutput, {
        claimToken: job.claim_token,
        idempotencyKey: `worker-complete-${job.job_id}-${Date.now()}`,
      })
      showToast?.('Job completed.', 'success')
      await refreshWorkerJobs()
    } catch (err) {
      setErrorFor(job.job_id, err?.message ?? 'Complete failed.')
    } finally {
      setLoadingFor(job.job_id, 'complete', false)
    }
  }

  const handleFail = async (job) => {
    clearErrorFor(job.job_id)
    setLoadingFor(job.job_id, 'fail', true)
    try {
      await failJob(apiKey, job.job_id, failDrafts[job.job_id] ?? '', {
        claimToken: job.claim_token,
        idempotencyKey: `worker-fail-${job.job_id}-${Date.now()}`,
      })
      showToast?.('Job marked failed.', 'success')
      await refreshWorkerJobs()
    } catch (err) {
      setErrorFor(job.job_id, err?.message ?? 'Fail request failed.')
    } finally {
      setLoadingFor(job.job_id, 'fail', false)
    }
  }

  const toggleThread = async (jobId) => {
    setThreadOpen(prev => ({ ...prev, [jobId]: !prev[jobId] }))
    if (!threadOpen[jobId]) await loadMessagesForJob(jobId)
  }

  const handleSendMessage = async (jobId) => {
    const draft = String(messageDrafts[jobId] ?? '').trim()
    if (!draft) return
    setLoadingFor(jobId, 'message', true)
    try {
      await postJobMessage(apiKey, jobId, { type: 'note', payload: { text: draft } })
      setMessageDrafts(prev => ({ ...prev, [jobId]: '' }))
      await loadMessagesForJob(jobId)
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to post message.', 'error')
    } finally {
      setLoadingFor(jobId, 'message', false)
    }
  }

  const pageLoading = loading || discovering || loadingJobs

  return (
    <main className="worker-page">
      <Topbar crumbs={[{ label: 'Worker' }]} />

      <div className="worker-page__scroll">
        <div className="worker-page__content">
          <Reveal>
            <header className="worker-page__header">
              <div>
                <p className="worker-page__eyebrow t-micro">Worker console</p>
                <h1>Claim + run jobs</h1>
                <p>Manage open jobs for your agent listings, keep leases alive, and settle with complete/fail actions.</p>
              </div>
              <Button
                variant="secondary"
                size="sm"
                loading={refreshingNow}
                onClick={async () => {
                  setRefreshingNow(true)
                  await refreshWorkerJobs()
                  setRefreshingNow(false)
                }}
              >
                Refresh now
              </Button>
            </header>
          </Reveal>

          <Reveal delay={0.05}>
            <section className="worker-page__summary">
              <div className="worker-page__summary-item">
                <span className="worker-page__summary-label">Owned agents</span>
                <span className="worker-page__summary-value">
                  {ownedAgentNames.length > 0 ? ownedAgentNames.join(', ') : 'None detected'}
                </span>
              </div>
              <div className="worker-page__summary-item">
                <span className="worker-page__summary-label">Open jobs</span>
                <span className="worker-page__summary-value">{workerJobs.length}</span>
              </div>
            </section>
          </Reveal>

          {pageLoading ? (
            <div className="worker-page__list">
              {[1, 2, 3, 4].map(i => <Skeleton key={i} variant="rect" height={250} />)}
            </div>
          ) : workerJobs.length === 0 ? (
            <EmptyState
              title="No open worker jobs"
              sub="Your owned agents currently have no pending/running jobs."
            />
          ) : (
            <div className="worker-page__list">
              {workerJobs.map(job => (
                <Card key={job.job_id}>
                  <Card.Header>
                    <div className="worker-page__card-head">
                      <div>
                        <p className="worker-page__job-agent">{job._agent_name}</p>
                        <p className="worker-page__job-id">{job.job_id}</p>
                      </div>
                      <div className="worker-page__badges">
                        <Badge label={job.status} dot />
                        <Badge label={`Lease: ${fmtLeaseRemaining(job.lease_expires_at, tickNow)}`} />
                      </div>
                    </div>
                  </Card.Header>
                  <Card.Body>
                    <div className="worker-page__meta">
                      <span>Created: {fmtDate(job.created_at)}</span>
                      <span>Claimed at: {fmtDate(job.claimed_at)}</span>
                      <span>Attempts: {job.attempt_count} / {job.max_attempts}</span>
                    </div>

                    <div className="worker-page__payloads">
                      <div>
                        <p className="worker-page__label">Input payload</p>
                        <pre className="worker-page__json">{JSON.stringify(job.input_payload ?? {}, null, 2)}</pre>
                      </div>
                      <div>
                        <p className="worker-page__label">Output payload (for complete)</p>
                        <Textarea
                          mono
                          value={outputDrafts[job.job_id] ?? JSON.stringify(job.output_payload ?? {}, null, 2)}
                          onChange={(e) => setOutputDrafts(prev => ({ ...prev, [job.job_id]: e.target.value }))}
                          style={{ minHeight: 110 }}
                        />
                      </div>
                    </div>

                    <div className="worker-page__payloads">
                      <div>
                        <p className="worker-page__label">Failure reason (optional)</p>
                        <Textarea
                          value={failDrafts[job.job_id] ?? ''}
                          onChange={(e) => setFailDrafts(prev => ({ ...prev, [job.job_id]: e.target.value }))}
                          style={{ minHeight: 80 }}
                          placeholder="Explain why this run failed…"
                        />
                      </div>
                    </div>

                    {actionErrors[job.job_id] && (
                      <div className="worker-page__action-error">
                        <span>{actionErrors[job.job_id]}</span>
                        <button type="button" className="worker-page__action-error-dismiss" onClick={() => clearErrorFor(job.job_id)}>✕</button>
                      </div>
                    )}

                    <div className="worker-page__actions">
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={isLoadingFor(job.job_id, 'claim')}
                        disabled={job.status === 'running'}
                        onClick={() => handleClaim(job)}
                      >
                        Claim
                      </Button>
                      <Button
                        size="sm"
                        variant="secondary"
                        loading={isLoadingFor(job.job_id, 'heartbeat')}
                        disabled={job.status !== 'running'}
                        onClick={() => handleHeartbeat(job)}
                      >
                        Heartbeat
                      </Button>
                      <Button
                        size="sm"
                        variant="primary"
                        loading={isLoadingFor(job.job_id, 'complete')}
                        disabled={job.status !== 'running'}
                        onClick={() => handleComplete(job)}
                      >
                        Complete
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={isLoadingFor(job.job_id, 'fail')}
                        disabled={job.status !== 'running'}
                        onClick={() => handleFail(job)}
                      >
                        Fail
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => toggleThread(job.job_id)}>
                        {threadOpen[job.job_id] ? 'Hide messages' : 'Messages'}
                      </Button>
                    </div>

                    {threadOpen[job.job_id] && (
                      <div className="worker-page__thread">
                        <p className="worker-page__label">Message thread</p>
                        {threadLoading[job.job_id] ? (
                          <p className="worker-page__muted">Loading messages…</p>
                        ) : (messagesByJob[job.job_id] ?? []).length === 0 ? (
                          <p className="worker-page__muted">No messages yet.</p>
                        ) : (
                          <div className="worker-page__messages">
                            {(messagesByJob[job.job_id] ?? []).map(message => (
                              <div key={message.message_id} className="worker-page__message">
                                <div className="worker-page__message-meta">
                                  <span>{message.type}</span>
                                  <span>{message.from_id}</span>
                                  <span>{fmtDate(message.created_at)}</span>
                                </div>
                                <pre className="worker-page__json">
                                  {JSON.stringify(message.payload ?? {}, null, 2)}
                                </pre>
                              </div>
                            ))}
                          </div>
                        )}
                        <div className="worker-page__message-compose">
                          <Textarea
                            value={messageDrafts[job.job_id] ?? ''}
                            onChange={(e) => setMessageDrafts(prev => ({ ...prev, [job.job_id]: e.target.value }))}
                            style={{ minHeight: 80 }}
                            placeholder="Post a note to this job thread…"
                          />
                          <div className="worker-page__message-actions">
                            <Button
                              size="sm"
                              variant="secondary"
                              loading={isLoadingFor(job.job_id, 'message')}
                              onClick={() => handleSendMessage(job.job_id)}
                            >
                              Send message
                            </Button>
                          </div>
                        </div>
                      </div>
                    )}
                  </Card.Body>
                </Card>
              ))}
            </div>
          )}
        </div>
      </div>
    </main>
  )
}
