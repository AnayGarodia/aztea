import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Reveal from '../ui/motion/Reveal'
import AgentSigil from '../brand/AgentSigil'
import ResultRenderer from '../features/agents/results/ResultRenderer'
import { getJob, getJobMessages, postJobMessage, rateJob, getJobDispute, fileDispute, verifyJob } from '../api'
import { useMarket } from '../context/MarketContext'
import JobTimeline from '../features/jobs/JobTimeline'
import { ArrowLeft, RefreshCw, Star, AlertTriangle, CheckCircle, Clock, RotateCcw } from 'lucide-react'
import './JobDetailPage.css'

function fmtCountdown(isoDeadline) {
  if (!isoDeadline) return null
  const diff = new Date(isoDeadline).getTime() - Date.now()
  if (diff <= 0) return 'Expired'
  const totalMins = Math.floor(diff / 60000)
  const hrs = Math.floor(totalMins / 60)
  const mins = totalMins % 60
  if (hrs > 0) return `${hrs}h ${mins}m`
  return `${mins}m`
}

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

const OUTCOME_LABELS = {
  caller_wins: 'Caller wins',
  agent_wins: 'Agent wins',
  split: 'Split',
  void: 'Void',
}

const DISPUTE_STATUS_COLORS = {
  pending: 'var(--warn-line, #f0d060)',
  judging: 'var(--accent)',
  consensus: 'var(--positive)',
  tied: 'var(--warn-line)',
  resolved: 'var(--positive)',
  appealed: 'var(--warn-line)',
  final: 'var(--positive)',
}

export default function JobDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { jobs, agents, apiKey, refreshJobs, showToast } = useMarket()
  const [localJob, setLocalJob] = useState(null)
  const [messages, setMessages] = useState([])
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [dispute, setDispute] = useState(undefined) // undefined=not fetched, null=none
  const [disputeReason, setDisputeReason] = useState('')
  const [disputeEvidence, setDisputeEvidence] = useState('')
  const [filingDispute, setFilingDispute] = useState(false)
  const [showDisputeForm, setShowDisputeForm] = useState(false)
  const [rating, setRating] = useState(null)
  const [ratingSubmitting, setRatingSubmitting] = useState(false)
  const [ratingDone, setRatingDone] = useState(false)
  const [clarificationAnswer, setClarificationAnswer] = useState('')
  const [clarificationSubmitting, setClarificationSubmitting] = useState(false)
  const [verifyLoading, setVerifyLoading] = useState(false)
  const [verifyDone, setVerifyDone] = useState(null) // 'accepted' | 'rejected'
  const [verifyConfirming, setVerifyConfirming] = useState(false)
  const [showRejectForm, setShowRejectForm] = useState(false)
  const [rejectReason, setRejectReason] = useState('')
  const [countdown, setCountdown] = useState(null)

  const contextJob = useMemo(() => jobs.find(j => j.job_id === id), [jobs, id])
  const job = localJob ?? contextJob
  const agent = useMemo(() => agents.find(a => a.agent_id === job?.agent_id), [agents, job])

  const loadMessages = async () => {
    if (!id || !apiKey) return
    setLoadingMsgs(true)
    try {
      const res = await getJobMessages(apiKey, id)
      setMessages(Array.isArray(res?.messages) ? res.messages : [])
    } catch (err) {
      // Non-fatal: keep whatever messages we had; only clear on first load
      if (messages.length === 0) setMessages([])
    } finally {
      setLoadingMsgs(false)
    }
  }

  const loadDispute = async () => {
    if (!id || !apiKey) return
    try {
      const d = await getJobDispute(apiKey, id)
      setDispute(d ?? null)
    } catch {
      if (dispute === undefined) setDispute(null)
    }
  }

  const TERMINAL = useMemo(() => new Set(['complete', 'failed', 'cancelled']), [])

  const pollJob = useCallback(async () => {
    if (!id || !apiKey) return
    try {
      const data = await getJob(apiKey, id)
      if (data?.job_id) setLocalJob(data)
      if (data?.status === 'complete' || data?.status === 'failed') {
        await loadMessages()
        if (data.status === 'complete') await loadDispute()
      }
    } catch {
      // Network blip during polling - keep stale data rather than clearing
    }
  }, [id, apiKey]) // eslint-disable-line

  useEffect(() => { loadMessages() }, [apiKey, id]) // eslint-disable-line
  useEffect(() => {
    if (job?.status === 'complete') loadDispute()
  }, [apiKey, id, job?.status]) // eslint-disable-line

  // Initial load
  useEffect(() => { pollJob() }, [pollJob])

  // Auto-poll every 3s while non-terminal
  const pollingRef = useRef(null)
  useEffect(() => {
    if (!id || !apiKey || TERMINAL.has(job?.status)) {
      if (pollingRef.current) { clearInterval(pollingRef.current); pollingRef.current = null }
      return
    }
    pollingRef.current = setInterval(() => {
      pollJob()
      loadMessages()
    }, 3000)
    return () => { if (pollingRef.current) clearInterval(pollingRef.current) }
  }, [id, apiKey, job?.status, TERMINAL, pollJob]) // eslint-disable-line

  const handleRefresh = async () => {
    setRefreshing(true)
    await Promise.all([pollJob(), loadMessages()])
    if (job?.status === 'complete') await loadDispute()
    setRefreshing(false)
  }

  const handleRating = async (stars) => {
    if (ratingDone || !apiKey) return
    setRating(stars)
    setRatingSubmitting(true)
    try {
      await rateJob(apiKey, id, stars)
      setRatingDone(true)
      showToast?.('Rating submitted.', 'success')
    } catch (e) {
      showToast?.(e?.message || 'Could not submit rating.', 'error')
      setRating(null)
    } finally {
      setRatingSubmitting(false)
    }
  }

  const handleFileDispute = async (e) => {
    e.preventDefault()
    if (!disputeReason.trim()) return
    setFilingDispute(true)
    try {
      const d = await fileDispute(apiKey, id, { reason: disputeReason, evidence: disputeEvidence, side: 'caller' })
      setDispute(d)
      setShowDisputeForm(false)
      showToast?.('Dispute filed. Our judges will review it shortly.', 'success')
    } catch (err) {
      showToast?.(err?.message || 'Could not file dispute.', 'error')
    } finally {
      setFilingDispute(false)
    }
  }

  const clarificationThread = useMemo(
    () => messages.filter(msg => ['clarification_request', 'clarification_response'].includes(msg.type)),
    [messages],
  )
  const latestClarificationRequest = useMemo(
    () => [...clarificationThread].reverse().find(msg => msg.type === 'clarification_request'),
    [clarificationThread],
  )
  const hasClarificationResponse = useMemo(() => {
    if (!latestClarificationRequest) return false
    return clarificationThread.some(
      msg =>
        msg.type === 'clarification_response'
        && String(msg?.payload?.request_message_id ?? '') === String(latestClarificationRequest.message_id),
    )
  }, [clarificationThread, latestClarificationRequest])

  const handleClarificationResponse = async (e) => {
    e.preventDefault()
    const answer = clarificationAnswer.trim()
    if (!latestClarificationRequest || !answer) return
    // Shadow-update the value so we use the trimmed version
    setClarificationAnswer(answer)
    setClarificationSubmitting(true)
    try {
      await postJobMessage(apiKey, id, {
        type: 'clarification_response',
        payload: {
          answer: clarificationAnswer.trim(),
          request_message_id: latestClarificationRequest.message_id,
        },
      })
      setClarificationAnswer('')
      await loadMessages()
      showToast?.('Clarification sent.', 'success')
    } catch (err) {
      showToast?.(err?.message || 'Could not send clarification response.', 'error')
    } finally {
      setClarificationSubmitting(false)
    }
  }

  useEffect(() => {
    const deadline = job?.output_verification_deadline_at
    if (!deadline || job?.output_verification_status !== 'pending') return
    const update = () => setCountdown(fmtCountdown(deadline))
    update()
    const interval = setInterval(update, 30000)
    return () => clearInterval(interval)
  }, [job?.output_verification_deadline_at, job?.output_verification_status])

  const handleVerify = async (decision) => {
    if (!apiKey) return
    setVerifyLoading(true)
    try {
      await verifyJob(apiKey, id, { decision, reason: decision === 'reject' ? rejectReason.trim() : undefined })
      setVerifyDone(decision)
      if (decision === 'accept') {
        showToast?.('Payment released - the agent has been paid.', 'success')
      } else {
        showToast?.('Output rejected - dispute opened.', 'success')
        await loadDispute()
      }
      await refreshJobs?.()
    } catch (err) {
      showToast?.(err?.message || 'Verification action failed.', 'error')
    } finally {
      setVerifyLoading(false)
    }
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
              <JobTimeline
                status={job.status}
                timestamps={{
                  pending: job.created_at,
                  running: job.claimed_at,
                  awaiting_clarification: job.clarification_requested_at,
                  complete: job.completed_at,
                  failed: job.completed_at,
                }}
              />
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
                  <InfoRow label="Attempts" value={`${job.attempt_count} / ${job.max_attempts ?? '-'}`} mono />
                )}
                <InfoRow label="Created" value={fmtDate(job.created_at)} />
                {job.completed_at && <InfoRow label="Completed" value={fmtDate(job.completed_at)} />}
                {job.error_message && (
                  <div className="job-detail__error-box">
                    <p className="job-detail__error-title">Error</p>
                    <p className="job-detail__error-msg">{job.error_message}</p>
                    {agent && (
                      <Button
                        variant="secondary"
                        size="sm"
                        icon={<RotateCcw size={13} />}
                        onClick={() => navigate(`/agents/${agent.agent_id}`, {
                          state: { prefillInput: job.input_payload },
                        })}
                        style={{ marginTop: 12 }}
                      >
                        Hire again
                      </Button>
                    )}
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

          {/* Verification panel */}
          {job.status === 'complete' && !verifyDone && job.output_verification_status === 'pending' && (
            <Reveal delay={0.22}>
              <Card className="job-detail__verify-card">
                <Card.Header>
                  <span className="job-detail__section-title">Verify Output</span>
                  {countdown && (
                    <span className="job-detail__verify-countdown">
                      <Clock size={12} />
                      Auto-accepts in {countdown}
                    </span>
                  )}
                </Card.Header>
                <Card.Body>
                  {!showRejectForm ? (
                    <div className="job-detail__verify-actions">
                      {verifyConfirming ? (
                        <div className="job-detail__verify-confirm">
                          <p className="job-detail__verify-confirm-msg">
                            Release payment to the agent? This is irreversible.
                          </p>
                          <div className="job-detail__verify-confirm-row">
                            <Button
                              variant="primary"
                              size="sm"
                              icon={<CheckCircle size={14} />}
                              onClick={() => handleVerify('accept')}
                              loading={verifyLoading}
                            >
                              Yes, release payment
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={verifyLoading}
                              onClick={() => setVerifyConfirming(false)}
                            >
                              Cancel
                            </Button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <Button
                            variant="primary"
                            size="sm"
                            icon={<CheckCircle size={14} />}
                            onClick={() => setVerifyConfirming(true)}
                            disabled={verifyLoading}
                          >
                            Accept &amp; Release Payment
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            icon={<AlertTriangle size={13} />}
                            onClick={() => setShowRejectForm(true)}
                            disabled={verifyLoading}
                          >
                            Reject &amp; Dispute
                          </Button>
                        </>
                      )}
                    </div>
                  ) : (
                    <form onSubmit={e => { e.preventDefault(); handleVerify('reject') }} className="job-detail__verify-reject-form">
                      <label className="job-detail__verify-label">
                        Reason <span className="job-detail__verify-required">*</span>
                      </label>
                      <textarea
                        required
                        rows={3}
                        value={rejectReason}
                        onChange={e => setRejectReason(e.target.value)}
                        placeholder="Describe what's wrong with the output."
                        className="job-detail__verify-textarea"
                      />
                      <div className="job-detail__verify-actions">
                        <Button type="submit" variant="danger" size="sm" loading={verifyLoading} disabled={!rejectReason.trim()}>
                          Reject &amp; Dispute
                        </Button>
                        <Button type="button" variant="secondary" size="sm" onClick={() => setShowRejectForm(false)} disabled={verifyLoading}>
                          Cancel
                        </Button>
                      </div>
                    </form>
                  )}
                </Card.Body>
              </Card>
            </Reveal>
          )}
          {verifyDone === 'accepted' && (
            <Reveal delay={0.22}>
              <div className="job-detail__verify-accepted">
                <CheckCircle size={15} />
                Payment released - the agent has been paid.
              </div>
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
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)', padding: 'var(--sp-2) 0' }}>
                    {[1,2,3].map(i => <Skeleton key={i} variant="rect" height={52} />)}
                  </div>
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

          <Reveal delay={0.27}>
            <Card>
              <Card.Header>
                <span className="job-detail__section-title">
                  Clarification thread {clarificationThread.length > 0 && `(${clarificationThread.length})`}
                </span>
              </Card.Header>
              <Card.Body>
                {clarificationThread.length === 0 ? (
                  <p className="job-detail__no-msg">No clarification messages yet.</p>
                ) : (
                  <div className="job-detail__messages">
                    {clarificationThread.map(msg => (
                      <MessageBubble key={`clar-${msg.message_id}`} msg={msg} />
                    ))}
                  </div>
                )}
                {latestClarificationRequest && !hasClarificationResponse && (
                  <form onSubmit={handleClarificationResponse} className="job-detail__clarification-form">
                    <p className="job-detail__clarification-note">
                      Respond to the latest clarification request to unblock this job.
                    </p>
                    <textarea
                      rows={3}
                      required
                      value={clarificationAnswer}
                      onChange={event => setClarificationAnswer(event.target.value)}
                      placeholder="Add clarification context for the worker."
                    />
                    <Button
                      type="submit"
                      variant="primary"
                      size="sm"
                      loading={clarificationSubmitting}
                      disabled={!clarificationAnswer.trim()}
                    >
                      Send clarification
                    </Button>
                  </form>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {/* Rating + Dispute - only for completed jobs */}
          {job.status === 'complete' && (
            <Reveal delay={0.3}>
              <Card>
                <Card.Header>
                  <span className="job-detail__section-title">Rate &amp; Dispute</span>
                </Card.Header>
                <Card.Body>
                  {/* Star rating */}
                  {!ratingDone && !dispute && (
                    <div style={{ marginBottom: 'var(--sp-4)' }}>
                      <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', marginBottom: 'var(--sp-2)' }}>
                        Rate this job (1–5). Submitting a rating closes the dispute window.
                      </p>
                      <div style={{ display: 'flex', gap: 'var(--sp-2)' }}>
                        {[1, 2, 3, 4, 5].map(s => (
                          <button
                            key={s}
                            disabled={ratingSubmitting}
                            onClick={() => handleRating(s)}
                            style={{
                              background: 'none', border: 'none', cursor: 'pointer', padding: '4px',
                              color: s <= (rating ?? 0) ? 'var(--warn-line, #f0c060)' : 'var(--line-mid)',
                              transition: 'color 0.15s',
                            }}
                          >
                            <Star size={22} fill={s <= (rating ?? 0) ? 'currentColor' : 'none'} />
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {ratingDone && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginBottom: 'var(--sp-4)', color: 'var(--positive)' }}>
                      <CheckCircle size={16} />
                      <span style={{ fontSize: '0.875rem' }}>Rating submitted - thank you.</span>
                    </div>
                  )}

                  {/* Dispute status */}
                  {dispute ? (
                    <div style={{
                      padding: 'var(--sp-4)',
                      border: `1px solid ${DISPUTE_STATUS_COLORS[dispute.status] || 'var(--line-mid)'}`,
                      borderRadius: 'var(--r-md)',
                      background: 'var(--surface-raised)',
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginBottom: 'var(--sp-2)' }}>
                        <AlertTriangle size={15} color={DISPUTE_STATUS_COLORS[dispute.status]} />
                        <span style={{ fontWeight: 600, fontSize: '0.875rem' }}>
                          Dispute · {dispute.status}
                        </span>
                        {dispute.outcome && (
                          <Badge label={OUTCOME_LABELS[dispute.outcome] || dispute.outcome} />
                        )}
                      </div>
                      <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', marginBottom: 'var(--sp-1)' }}>
                        <strong>Reason:</strong> {dispute.reason}
                      </p>
                      {dispute.evidence && (
                        <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)' }}>
                          <strong>Evidence:</strong> {dispute.evidence}
                        </p>
                      )}
                      {dispute.status === 'pending' && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginTop: 'var(--sp-3)', color: 'var(--ink-mute)', fontSize: '0.75rem' }}>
                          <Clock size={13} />
                          Under review - typically resolved within 24 hours.
                        </div>
                      )}
                      {dispute.judgments?.length > 0 && (
                        <div style={{ marginTop: 'var(--sp-3)', borderTop: '1px solid var(--line-soft)', paddingTop: 'var(--sp-3)' }}>
                          <p style={{ fontSize: '0.75rem', fontWeight: 600, marginBottom: 'var(--sp-2)', color: 'var(--ink-mute)' }}>Judgments</p>
                          {dispute.judgments.map((j, i) => (
                            <div key={i} style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', marginBottom: 'var(--sp-1)' }}>
                              <Badge label={j.judge_kind} /> {OUTCOME_LABELS[j.verdict] || j.verdict} - {j.reasoning}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ) : !ratingDone && (
                    <>
                      {showDisputeForm ? (
                        <form onSubmit={handleFileDispute} style={{ marginTop: 'var(--sp-3)' }}>
                          <div style={{ marginBottom: 'var(--sp-3)' }}>
                            <label style={{ display: 'block', fontSize: '0.8125rem', fontWeight: 600, marginBottom: 'var(--sp-1)' }}>
                              Reason <span style={{ color: 'var(--negative)' }}>*</span>
                            </label>
                            <textarea
                              required
                              rows={3}
                              value={disputeReason}
                              onChange={e => setDisputeReason(e.target.value)}
                              placeholder="Describe what went wrong - wrong output, no response, etc."
                              style={{
                                width: '100%', padding: 'var(--sp-2) var(--sp-3)',
                                border: '1px solid var(--line-mid)', borderRadius: 'var(--r-sm)',
                                fontSize: '0.875rem', resize: 'vertical', background: 'var(--surface)',
                                color: 'var(--ink)', boxSizing: 'border-box',
                              }}
                            />
                          </div>
                          <div style={{ marginBottom: 'var(--sp-3)' }}>
                            <label style={{ display: 'block', fontSize: '0.8125rem', fontWeight: 600, marginBottom: 'var(--sp-1)' }}>
                              Evidence (optional)
                            </label>
                            <textarea
                              rows={2}
                              value={disputeEvidence}
                              onChange={e => setDisputeEvidence(e.target.value)}
                              placeholder="Paste relevant output, logs, or context that supports your case."
                              style={{
                                width: '100%', padding: 'var(--sp-2) var(--sp-3)',
                                border: '1px solid var(--line-mid)', borderRadius: 'var(--r-sm)',
                                fontSize: '0.875rem', resize: 'vertical', background: 'var(--surface)',
                                color: 'var(--ink)', boxSizing: 'border-box',
                              }}
                            />
                          </div>
                          <div style={{ display: 'flex', gap: 'var(--sp-2)' }}>
                            <Button type="submit" variant="danger" size="sm" loading={filingDispute}>
                              Submit dispute
                            </Button>
                            <Button type="button" variant="secondary" size="sm" onClick={() => setShowDisputeForm(false)}>
                              Cancel
                            </Button>
                          </div>
                        </form>
                      ) : (
                        <Button
                          variant="secondary"
                          size="sm"
                          icon={<AlertTriangle size={13} />}
                          onClick={() => setShowDisputeForm(true)}
                          style={{ marginTop: 'var(--sp-2)' }}
                        >
                          File a dispute
                        </Button>
                      )}
                    </>
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
