import { useState, useEffect, useCallback } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Reveal from '../ui/motion/Reveal'
import { fetchAdminDisputes, fetchAdminDispute, ruleDispute } from '../api'
import { useAuth } from '../context/AuthContext'
import { ChevronDown, ChevronUp, Scale } from 'lucide-react'
import './AdminDisputesPage.css'
import { fmtUsd } from '../utils/format.js'

function relativeAge(isoString) {
  if (!isoString) return '-'
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h`
  const days = Math.floor(hrs / 24)
  return `${days}d`
}

function shortId(id) {
  if (!id) return '-'
  const parts = String(id).split(':')
  const base = parts[parts.length - 1]
  return base.length > 10 ? base.slice(0, 10) + '…' : base
}

const STATUS_VARIANT = {
  pending: 'warning',
  tied: 'error',
  judging: 'info',
  consensus: 'success',
  resolved: 'success',
  appealed: 'warning',
  final: 'success',
}

const OUTCOMES = [
  { value: 'caller_wins', label: 'Caller wins' },
  { value: 'agent_wins', label: 'Agent wins' },
  { value: 'split', label: 'Split' },
  { value: 'void', label: 'Void (full caller refund)' },
]

const STATUS_FILTERS = [
  { value: '', label: 'All' },
  { value: 'pending', label: 'Pending' },
  { value: 'tied', label: 'Tied' },
  { value: 'consensus', label: 'Consensus' },
  { value: 'final', label: 'Final' },
]

function RulingPanel({ disputeId, priceCents, onRuled, apiKey }) {
  const [outcome, setOutcome] = useState('caller_wins')
  const [reason, setReason] = useState('')
  const [callerCents, setCallerCents] = useState('')
  const [agentCents, setAgentCents] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const callerVal = parseInt(callerCents, 10) || 0
  const agentVal = parseInt(agentCents, 10) || 0
  const platformVal = priceCents - callerVal - agentVal
  const splitInvalid = outcome === 'split' && (
    callerVal < 0 || agentVal < 0 || callerVal + agentVal > priceCents
  )

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!reason.trim()) return
    if (splitInvalid) return
    setLoading(true)
    setError(null)
    try {
      const result = await ruleDispute(apiKey, disputeId, {
        outcome,
        reasoning: reason.trim(),
        split_caller_cents: outcome === 'split' ? callerVal : undefined,
        split_agent_cents: outcome === 'split' ? agentVal : undefined,
      })
      onRuled(result)
    } catch (err) {
      setError(err?.message || 'Ruling failed.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="adp__ruling-form">
      <p className="adp__ruling-title">
        <Scale size={13} />
        Rule this dispute
      </p>

      <div className="adp__ruling-outcomes">
        {OUTCOMES.map(o => (
          <label key={o.value} className={`adp__ruling-radio${outcome === o.value ? ' adp__ruling-radio--active' : ''}`}>
            <input
              type="radio"
              name="outcome"
              value={o.value}
              checked={outcome === o.value}
              onChange={() => setOutcome(o.value)}
            />
            {o.label}
          </label>
        ))}
      </div>

      {outcome === 'split' && (
        <div className="adp__split-fields">
          <div className="adp__split-field">
            <label className="adp__split-label">Caller gets (¢)</label>
            <input
              type="number"
              min={0}
              max={priceCents}
              value={callerCents}
              onChange={e => setCallerCents(e.target.value)}
              className="adp__split-input"
              placeholder="0"
            />
          </div>
          <div className="adp__split-field">
            <label className="adp__split-label">Agent gets (¢)</label>
            <input
              type="number"
              min={0}
              max={priceCents}
              value={agentCents}
              onChange={e => setAgentCents(e.target.value)}
              className="adp__split-input"
              placeholder="0"
            />
          </div>
          <div className="adp__split-remainder">
            Platform: {platformVal}¢
            {splitInvalid && <span className="adp__split-err"> - exceeds job value ({priceCents}¢)</span>}
          </div>
        </div>
      )}

      <div className="adp__ruling-reason-wrap">
        <label className="adp__ruling-label">
          Reasoning <span className="adp__ruling-required">*</span>
        </label>
        <textarea
          required
          rows={3}
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="Explain your ruling for the record."
          className="adp__ruling-textarea"
        />
      </div>

      {error && <p className="adp__ruling-error">{error}</p>}

      <Button
        type="submit"
        variant="primary"
        size="sm"
        loading={loading}
        disabled={!reason.trim() || splitInvalid}
      >
        Submit ruling
      </Button>
    </form>
  )
}

function DisputeDetail({ disputeId, apiKey, priceCents, onRuled, showRuling }) {
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true
    setLoading(true)
    fetchAdminDispute(apiKey, disputeId)
      .then(d => { if (active) setDetail(d) })
      .catch(() => {})
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [apiKey, disputeId])

  if (loading) return <div className="adp__detail-loading">Loading…</div>
  if (!detail) return <div className="adp__detail-loading">Failed to load.</div>

  const { dispute, job } = detail
  const judgments = detail.judgments ?? []
  const escrow = detail.escrow_balance_cents ?? 0

  return (
    <div className="adp__detail">
      <div className="adp__detail-grid">
        {/* Complaint */}
        <div className="adp__detail-section">
          <p className="adp__detail-label">Reason</p>
          <p className="adp__detail-text">{dispute?.reason || ' - '}</p>
          {dispute?.evidence && (
            <>
              <p className="adp__detail-label" style={{ marginTop: 8 }}>Evidence</p>
              <p className="adp__detail-text">{dispute.evidence}</p>
            </>
          )}
        </div>

        {/* Financials */}
        <div className="adp__detail-section">
          <p className="adp__detail-label">Escrow balance</p>
          <p className="adp__detail-value">{fmtUsd(escrow)}</p>
          <p className="adp__detail-label" style={{ marginTop: 8 }}>Job value</p>
          <p className="adp__detail-value">{fmtUsd(job?.price_cents)}</p>
        </div>
      </div>

      {/* Judgments */}
      {judgments.length > 0 && (
        <div className="adp__detail-section">
          <p className="adp__detail-label">Judge verdicts</p>
          {judgments.map((j, i) => (
            <div key={i} className="adp__judgment">
              <span className="adp__judgment-kind">{j.judge_kind}</span>
              <span className="adp__judgment-verdict">{j.verdict?.replace('_', ' ')}</span>
              {j.reasoning && <p className="adp__judgment-reason">{j.reasoning}</p>}
            </div>
          ))}
        </div>
      )}

      {/* Input / Output */}
      {job?.input_payload && (
        <div className="adp__detail-section">
          <p className="adp__detail-label">Job input</p>
          <pre className="adp__detail-pre">{JSON.stringify(job.input_payload, null, 2)}</pre>
        </div>
      )}
      {job?.output_payload && (
        <div className="adp__detail-section">
          <p className="adp__detail-label">Job output</p>
          <pre className="adp__detail-pre">{JSON.stringify(job.output_payload, null, 2)}</pre>
        </div>
      )}

      {/* Ruling panel */}
      {showRuling && (
        <RulingPanel
          disputeId={disputeId}
          priceCents={priceCents}
          onRuled={onRuled}
          apiKey={apiKey}
        />
      )}
    </div>
  )
}

export default function AdminDisputesPage() {
  const { apiKey } = useAuth()
  const [disputes, setDisputes] = useState([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState('')
  const [expandedId, setExpandedId] = useState(null)

  const load = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    try {
      const data = await fetchAdminDisputes(apiKey, { status: statusFilter || undefined })
      setDisputes(data?.disputes ?? [])
    } catch {
      setDisputes([])
    } finally {
      setLoading(false)
    }
  }, [apiKey, statusFilter])

  useEffect(() => { load() }, [load])

  const handleRuled = (result, disputeId) => {
    const updated = result?.dispute
    if (!updated) return
    setDisputes(prev => prev.map(d =>
      d.dispute_id === disputeId ? { ...d, ...updated } : d
    ))
    setExpandedId(null)
  }

  return (
    <main className="adp">
      <Topbar crumbs={[{ label: 'Admin' }, { label: 'Disputes' }]} />
      <div className="adp__scroll">
        <div className="adp__content">

          <Reveal>
            <div className="adp__header">
              <h1 className="adp__title">
                Dispute queue
              </h1>
              <div className="adp__filters">
                {STATUS_FILTERS.map(f => (
                  <button
                    key={f.value}
                    className={`adp__filter-btn${statusFilter === f.value ? ' adp__filter-btn--active' : ''}`}
                    onClick={() => { setStatusFilter(f.value); setExpandedId(null) }}
                    type="button"
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
          </Reveal>

          <Reveal delay={0.05}>
            <Card>
              <Card.Body>
                {loading ? (
                  <div className="adp__loading">Loading disputes…</div>
                ) : disputes.length === 0 ? (
                  <div className="adp__empty">No disputes match the current filter.</div>
                ) : (
                  <div className="adp__table">
                    <div className="adp__table-head">
                      <span>Age</span>
                      <span>Status</span>
                      <span>Value</span>
                      <span>Caller</span>
                      <span>Worker</span>
                      <span>LLM verdict</span>
                      <span />
                    </div>
                    {disputes.map(d => {
                      const isOpen = expandedId === d.dispute_id
                      const needsRuling = d.status === 'tied' || d.status === 'pending'
                      return (
                        <div key={d.dispute_id} className={`adp__row-wrap${isOpen ? ' adp__row-wrap--open' : ''}`}>
                          <button
                            className="adp__row"
                            onClick={() => setExpandedId(isOpen ? null : d.dispute_id)}
                            type="button"
                          >
                            <span className="adp__cell adp__cell--age">{relativeAge(d.filed_at)}</span>
                            <span className="adp__cell">
                              <Badge label={d.status} variant={STATUS_VARIANT[d.status]} dot />
                            </span>
                            <span className="adp__cell adp__cell--mono">{fmtUsd(d.price_cents)}</span>
                            <span className="adp__cell adp__cell--id">{shortId(d.caller_owner_id)}</span>
                            <span className="adp__cell adp__cell--id">{shortId(d.agent_owner_id)}</span>
                            <span className={`adp__cell adp__cell--verdict${needsRuling ? ' adp__cell--needs-ruling' : ''}`}>
                              {d.verdict_summary}
                            </span>
                            <span className="adp__cell adp__cell--chevron">
                              {isOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                            </span>
                          </button>
                          <AnimatePresence>
                            {isOpen && (
                              <motion.div
                                className="adp__expand"
                                initial={{ height: 0, opacity: 0 }}
                                animate={{ height: 'auto', opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }}
                                transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
                              >
                                <DisputeDetail
                                  disputeId={d.dispute_id}
                                  apiKey={apiKey}
                                  priceCents={d.price_cents}
                                  showRuling={d.status !== 'final'}
                                  onRuled={result => handleRuled(result, d.dispute_id)}
                                />
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </div>
                      )
                    })}
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
