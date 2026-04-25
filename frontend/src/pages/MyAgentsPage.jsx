import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import Stat from '../ui/Stat'
import EmptyState from '../ui/EmptyState'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import {
  fetchMyAgents,
  fetchAgentWallets,
  updateAgent,
  delistAgent,
  updateAgentWalletSettings,
  sweepAgentWallet,
  createAgentCallerKey,
} from '../api'
import { useAuth } from '../context/AuthContext'
import {
  Plus, Bot, ExternalLink, ChevronDown, Edit2, Trash2, Play, Copy, Check, X,
  Wallet, ArrowUpFromLine, Settings, KeyRound,
} from 'lucide-react'
import './MyAgentsPage.css'

function fmtCents(cents) {
  if (typeof cents !== 'number') return '$0.00'
  return '$' + (cents / 100).toFixed(2)
}

const STATUS_VARIANT = {
  active: 'success',
  suspended: 'warning',
  banned: 'error',
}

function fmtUsd(val) {
  if (typeof val !== 'number') return '-'
  return '$' + val.toFixed(4).replace(/\.?0+$/, '')
}

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

function completionVariant(rate) {
  if (rate === null || rate === undefined) return ''
  if (rate >= 0.8) return 'positive'
  if (rate >= 0.6) return 'warn'
  return 'negative'
}

function fmtCompletion(rate) {
  if (rate === null || rate === undefined) return '--'
  return `${Math.round(rate * 100)}%`
}

function fmtLatency(sec) {
  if (sec === null || sec === undefined) return '--'
  return `${sec}s`
}

function EditModal({ agent, onSave, onClose }) {
  const [name, setName] = useState(agent.name ?? '')
  const [description, setDescription] = useState(agent.description ?? '')
  const [price, setPrice] = useState(String(agent.price_per_call_usd ?? ''))
  const [tags, setTags] = useState(
    Array.isArray(agent.tags) ? agent.tags.join(', ')
    : typeof agent.tags === 'string' ? JSON.parse(agent.tags || '[]').join(', ')
    : ''
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const handleSave = async (e) => {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      const tagList = tags.split(',').map(t => t.trim()).filter(Boolean)
      const priceNum = parseFloat(price)
      if (isNaN(priceNum) || priceNum < 0) {
        setError('Price must be a non-negative number.')
        return
      }
      await onSave({ name: name.trim(), description: description.trim(), tags: tagList, price_per_call_usd: priceNum })
    } catch (err) {
      setError(err?.message || 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="myagents__modal-backdrop" onClick={onClose}>
      <div className="myagents__modal" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="myagents__modal-head">
          <span className="myagents__modal-title">Edit agent</span>
          <button type="button" className="myagents__modal-close" onClick={onClose} aria-label="Close"><X size={14} /></button>
        </div>
        <form className="myagents__modal-body" onSubmit={handleSave}>
          <Input label="Name" value={name} onChange={e => setName(e.target.value)} required maxLength={80} />
          <div className="myagents__modal-field">
            <label className="myagents__modal-label">Description</label>
            <textarea
              className="myagents__modal-textarea"
              value={description}
              onChange={e => setDescription(e.target.value)}
              maxLength={1000}
              rows={3}
            />
          </div>
          <Input
            label="Tags (comma-separated)"
            value={tags}
            onChange={e => setTags(e.target.value)}
            placeholder="research, api, tool"
          />
          <Input
            label="Price per call (USD)"
            type="number"
            value={price}
            onChange={e => setPrice(e.target.value)}
            min={0}
            step="0.0001"
          />
          {error && <p className="myagents__modal-error">{error}</p>}
          <div className="myagents__modal-actions">
            <Button type="button" variant="secondary" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" size="sm" loading={saving}>Save changes</Button>
          </div>
        </form>
      </div>
    </div>
  )
}

function WalletSettingsModal({ agent, wallet, onSave, onClose }) {
  const [label, setLabel] = useState(wallet?.display_label || agent.name || '')
  const [dailyLimit, setDailyLimit] = useState(
    wallet?.daily_spend_limit_cents != null ? String(wallet.daily_spend_limit_cents) : ''
  )
  const [guarantorOn, setGuarantorOn] = useState(Boolean(wallet?.guarantor_enabled))
  const [guarantorCap, setGuarantorCap] = useState(
    wallet?.guarantor_cap_cents != null ? String(wallet.guarantor_cap_cents) : ''
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const handleSave = async (e) => {
    e.preventDefault()
    setSaving(true)
    setError('')
    try {
      const payload = {
        display_label: label.trim() || null,
        guarantor_enabled: guarantorOn,
      }
      if (dailyLimit.trim() !== '') {
        const n = parseInt(dailyLimit, 10)
        if (Number.isNaN(n) || n < 0) { setError('Daily limit must be a non-negative integer.'); return }
        payload.daily_spend_limit_cents = n
      }
      if (guarantorCap.trim() !== '') {
        const n = parseInt(guarantorCap, 10)
        if (Number.isNaN(n) || n < 0) { setError('Guarantor cap must be a non-negative integer.'); return }
        payload.guarantor_cap_cents = n
      }
      await onSave(payload)
    } catch (err) {
      setError(err?.message || 'Save failed.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="myagents__modal-backdrop" onClick={onClose}>
      <div className="myagents__modal" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="myagents__modal-head">
          <span className="myagents__modal-title">Wallet settings</span>
          <button type="button" className="myagents__modal-close" onClick={onClose} aria-label="Close"><X size={14} /></button>
        </div>
        <form className="myagents__modal-body" onSubmit={handleSave}>
          <Input
            label="Wallet label"
            value={label}
            onChange={e => setLabel(e.target.value)}
            placeholder="e.g. Production code reviewer"
            maxLength={80}
          />
          <Input
            label="Daily spend limit (cents) — leave blank for no cap"
            type="number"
            value={dailyLimit}
            onChange={e => setDailyLimit(e.target.value)}
            min={0}
            step={1}
          />
          <div className="myagents__modal-field">
            <label className="myagents__guarantor-toggle">
              <input
                type="checkbox"
                checked={guarantorOn}
                onChange={e => setGuarantorOn(e.target.checked)}
              />
              <span>Owner backstop (used when this agent hires other agents)</span>
            </label>
            <p className="myagents__modal-hint">
              When enabled, charges that exceed this agent's balance will draw from your owner wallet, up to the cap below per UTC day.
              (Phase 2 — currently stored but not enforced.)
            </p>
          </div>
          <Input
            label="Backstop cap (cents per day)"
            type="number"
            value={guarantorCap}
            onChange={e => setGuarantorCap(e.target.value)}
            min={0}
            step={1}
            disabled={!guarantorOn}
          />
          {error && <p className="myagents__modal-error">{error}</p>}
          <div className="myagents__modal-actions">
            <Button type="button" variant="secondary" size="sm" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" size="sm" loading={saving}>Save settings</Button>
          </div>
        </form>
      </div>
    </div>
  )
}

function AgentRow({ agent, earnings, onNavigate, onRefresh, apiKey }) {
  const [open, setOpen] = useState(false)
  const [editOpen, setEditOpen] = useState(false)
  const [walletSettingsOpen, setWalletSettingsOpen] = useState(false)
  const [sweeping, setSweeping] = useState(false)
  const [delisting, setDelisting] = useState(false)
  const [confirmDelist, setConfirmDelist] = useState(false)
  const [confirmSweep, setConfirmSweep] = useState(false)
  const [copied, setCopied] = useState(false)
  const [callerKey, setCallerKey] = useState(null) // {raw_key, ...}
  const [mintingCallerKey, setMintingCallerKey] = useState(false)
  const [callerKeyCopied, setCallerKeyCopied] = useState(false)

  const tags = Array.isArray(agent.tags)
    ? agent.tags
    : (typeof agent.tags === 'string' ? JSON.parse(agent.tags || '[]') : [])
  const status = agent.status ?? 'active'
  const isProblematic = status === 'suspended' || status === 'banned'

  const earnedCents = earnings?.total_earned_cents ?? null
  const earnedFmt = typeof earnedCents === 'number'
    ? '$' + (earnedCents / 100).toFixed(2)
    : '--'
  const balanceCents = earnings?.current_balance_cents ?? 0
  const hasWallet = Boolean(earnings?.wallet_id)

  const handleSaveWalletSettings = async (data) => {
    await updateAgentWalletSettings(apiKey, agent.agent_id, data)
    setWalletSettingsOpen(false)
    onRefresh()
  }

  const handleMintCallerKey = async () => {
    setMintingCallerKey(true)
    try {
      const data = await createAgentCallerKey(apiKey, agent.agent_id, `${agent.name} caller`)
      setCallerKey(data)
    } catch {
      // surfaced via UI; let user retry
    } finally {
      setMintingCallerKey(false)
    }
  }

  const handleCopyCallerKey = async () => {
    if (!callerKey) return
    try {
      await navigator.clipboard.writeText(callerKey.raw_key)
      setCallerKeyCopied(true)
      setTimeout(() => setCallerKeyCopied(false), 2000)
    } catch { /* ignore */ }
  }

  const handleSweep = async () => {
    if (!confirmSweep) { setConfirmSweep(true); return }
    setSweeping(true)
    try {
      await sweepAgentWallet(apiKey, agent.agent_id)
      setConfirmSweep(false)
      onRefresh()
    } catch {
      // surfaced via re-fetch; keep button enabled to retry
    } finally {
      setSweeping(false)
    }
  }

  const handleCopyId = async () => {
    try {
      await navigator.clipboard.writeText(agent.agent_id)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // ignore
    }
  }

  const handleSaveEdit = async (data) => {
    await updateAgent(apiKey, agent.agent_id, data)
    setEditOpen(false)
    onRefresh()
  }

  const handleDelist = async () => {
    if (!confirmDelist) { setConfirmDelist(true); return }
    setDelisting(true)
    try {
      await delistAgent(apiKey, agent.agent_id)
      onRefresh()
    } catch {
      setDelisting(false)
      setConfirmDelist(false)
    }
  }

  return (
    <motion.div
      className="myagents__row-wrap"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: prefersReducedMotion() ? 0 : 0.2 }}
    >
      <div className="myagents__row-header">
        <div
          className="myagents__row"
          role="button"
          tabIndex={0}
          onClick={onNavigate}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onNavigate() }
          }}
        >
          <div className="myagents__row-icon">
            <Bot size={15} color="var(--accent)" />
          </div>
          <div className="myagents__row-main">
            <p className="myagents__row-name">{agent.name}</p>
            <p className="myagents__row-desc">{agent.description}</p>
            {isProblematic && agent.suspension_reason && (
              <p className="myagents__row-reason">{agent.suspension_reason}</p>
            )}
            {tags.length > 0 && (
              <div className="myagents__row-tags">
                {tags.slice(0, 4).map(t => (
                  <span key={t} className="myagents__row-tag">{t}</span>
                ))}
              </div>
            )}
          </div>
          <div className="myagents__row-meta">
            <Badge label={status} variant={STATUS_VARIANT[status] ?? 'default'} dot />
            <span className="myagents__row-price">{fmtUsd(agent.price_per_call_usd)} / call</span>
          </div>
        </div>

        <button
          className="myagents__expand-btn"
          onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
          aria-label={open ? 'Collapse' : 'Expand'}
          aria-expanded={open}
          type="button"
        >
          <ChevronDown
            size={14}
            className={`myagents__expand-icon${open ? ' myagents__expand-icon--open' : ''}`}
          />
        </button>
      </div>

      <AnimatePresence>
        {open && (
          <motion.div
            className="myagents__panel"
            id={`analytics-panel-${agent.agent_id}`}
            key="panel"
            initial={prefersReducedMotion() ? false : { height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={prefersReducedMotion() ? undefined : { height: 0, opacity: 0 }}
            transition={{ duration: prefersReducedMotion() ? 0 : 0.25, ease: [0.16, 1, 0.3, 1] }}
          >
            <div className="myagents__panel-inner">
              <div className="myagents__panel-grid">
                <Stat label="Total calls" value={agent.total_calls ?? '--'} />
                <Stat
                  label="30d completion"
                  value={fmtCompletion(agent.job_completion_rate)}
                  variant={completionVariant(agent.job_completion_rate)}
                />
                <Stat label="Median latency" value={fmtLatency(agent.median_latency_seconds)} />
                <Stat
                  label="Revenue earned"
                  value={earnedFmt}
                  variant={typeof earnedCents === 'number' && earnedCents > 0 ? 'positive' : ''}
                />
              </div>

              {/* Agent wallet */}
              <div className="myagents__wallet">
                <div className="myagents__wallet-head">
                  <span className="myagents__wallet-icon"><Wallet size={13} /></span>
                  <span className="myagents__wallet-label">Wallet</span>
                  {hasWallet && (
                    <button
                      type="button"
                      className="myagents__wallet-settings-btn"
                      onClick={() => setWalletSettingsOpen(true)}
                      title="Wallet settings"
                    >
                      <Settings size={12} />
                    </button>
                  )}
                </div>
                <div className="myagents__wallet-row">
                  <div className="myagents__wallet-balance">
                    <span className="myagents__wallet-balance-value">{fmtCents(balanceCents)}</span>
                    <span className="myagents__wallet-balance-sub">current balance</span>
                  </div>
                  <Button
                    size="sm"
                    variant={confirmSweep ? 'primary' : 'secondary'}
                    icon={<ArrowUpFromLine size={12} />}
                    loading={sweeping}
                    disabled={!hasWallet || balanceCents <= 0}
                    onClick={handleSweep}
                  >
                    {confirmSweep ? `Confirm sweep ${fmtCents(balanceCents)}` : 'Sweep to my wallet'}
                  </Button>
                  {confirmSweep && (
                    <button
                      type="button"
                      className="myagents__cancel-delist"
                      onClick={() => setConfirmSweep(false)}
                    >
                      Cancel
                    </button>
                  )}
                  <Button
                    size="sm"
                    variant="secondary"
                    icon={<KeyRound size={12} />}
                    loading={mintingCallerKey}
                    disabled={!hasWallet || Boolean(callerKey)}
                    onClick={handleMintCallerKey}
                    title="Mint a key this agent can use to hire other agents"
                  >
                    Mint caller key
                  </Button>
                </div>
                {callerKey && (
                  <div className="myagents__caller-key">
                    <div className="myagents__caller-key-head">
                      <span className="myagents__caller-key-label">
                        Save this — it will not be shown again.
                      </span>
                      <button
                        type="button"
                        className="myagents__modal-close"
                        onClick={() => setCallerKey(null)}
                        aria-label="Dismiss"
                      ><X size={12} /></button>
                    </div>
                    <code className="myagents__caller-key-value">{callerKey.raw_key}</code>
                    <button
                      type="button"
                      className="myagents__copy-id"
                      onClick={handleCopyCallerKey}
                    >
                      {callerKeyCopied ? <Check size={12} /> : <Copy size={12} />}
                      <span>{callerKeyCopied ? 'Copied' : 'Copy'}</span>
                    </button>
                  </div>
                )}
              </div>

              {/* Management actions */}
              <div className="myagents__actions">
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<Play size={12} />}
                  onClick={() => onNavigate()}
                >
                  Test / invoke
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  icon={<Edit2 size={12} />}
                  onClick={() => setEditOpen(true)}
                >
                  Edit
                </Button>
                <button
                  type="button"
                  className="myagents__copy-id"
                  onClick={handleCopyId}
                  title="Copy agent ID"
                >
                  {copied ? <Check size={12} /> : <Copy size={12} />}
                  <span>{copied ? 'Copied' : agent.agent_id.slice(0, 8) + '…'}</span>
                </button>
                {status !== 'deleted' && (
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={<Trash2 size={12} />}
                    loading={delisting}
                    onClick={handleDelist}
                    style={{ color: confirmDelist ? 'var(--negative)' : undefined }}
                  >
                    {confirmDelist ? 'Confirm delist' : 'Delist'}
                  </Button>
                )}
                {confirmDelist && (
                  <button
                    type="button"
                    className="myagents__cancel-delist"
                    onClick={() => setConfirmDelist(false)}
                  >
                    Cancel
                  </button>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {editOpen && (
        <EditModal
          agent={agent}
          onSave={handleSaveEdit}
          onClose={() => setEditOpen(false)}
        />
      )}

      {walletSettingsOpen && (
        <WalletSettingsModal
          agent={agent}
          wallet={earnings}
          onSave={handleSaveWalletSettings}
          onClose={() => setWalletSettingsOpen(false)}
        />
      )}
    </motion.div>
  )
}

export default function MyAgentsPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [agents, setAgents] = useState([])
  const [earningsMap, setEarningsMap] = useState({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    setError(null)
    try {
      const [agentsData, walletsData] = await Promise.all([
        fetchMyAgents(apiKey),
        fetchAgentWallets(apiKey),
      ])
      setAgents(agentsData?.agents ?? [])
      const map = {}
      for (const row of (walletsData?.agents ?? [])) {
        map[row.agent_id] = row
      }
      setEarningsMap(map)
    } catch (err) {
      setError(err?.message || 'Failed to load agents.')
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { load() }, [load])

  const earningsRows = Object.values(earningsMap)
  const totalRevenueCents = earningsRows.reduce((sum, r) => sum + (r.total_earned_cents ?? 0), 0)
  const totalCalls = earningsRows.reduce((sum, r) => sum + (r.call_count ?? 0), 0)
  const activeAgentCount = agents.filter(a => (a.status ?? 'active') === 'active').length

  return (
    <main className="myagents">
      <Topbar crumbs={[{ label: 'My Agents' }]} />
      <div className="myagents__scroll">
        <div className="myagents__content">

          <Reveal>
            <div className="myagents__header">
              <div>
                <h1 className="myagents__title">My Agents</h1>
                <p className="myagents__sub">Agents you've listed on the marketplace.</p>
              </div>
              <Button
                variant="primary"
                size="sm"
                icon={<Plus size={14} />}
                onClick={() => navigate('/list-skill')}
              >
                List a skill
              </Button>
            </div>
          </Reveal>

          {(agents.length > 0 || loading) && (
            <Reveal delay={0.05}>
              <Card>
                <Card.Body>
                  <div style={{ display: 'flex', gap: 'var(--sp-4)' }}>
                    <div style={{ flex: 1 }}>
                      <Stat
                        label="Total revenue"
                        value={loading ? '-' : '$' + (totalRevenueCents / 100).toFixed(2)}
                        variant={!loading && totalRevenueCents > 0 ? 'positive' : ''}
                      />
                    </div>
                    <div style={{ flex: 1 }}>
                      <Stat label="Total calls" value={loading ? '-' : totalCalls.toLocaleString()} />
                    </div>
                    <div style={{ flex: 1 }}>
                      <Stat label="Active agents" value={loading ? '-' : activeAgentCount} />
                    </div>
                  </div>
                </Card.Body>
              </Card>
            </Reveal>
          )}

          <Reveal delay={0.08}>
            <Card>
              <Card.Body>
                {loading ? (
                  <div className="myagents__skeleton">
                    {[1, 2, 3].map(i => <Skeleton key={i} variant="rect" height={80} />)}
                  </div>
                ) : error ? (
                  <div className="myagents__error">{error}</div>
                ) : agents.length === 0 ? (
                  <EmptyState
                    title="No skills listed yet"
                    sub="Upload a SKILL.md, set a price per call, and Aztea handles execution and billing for you."
                    action={
                      <Button
                        variant="primary"
                        size="sm"
                        icon={<Plus size={14} />}
                        onClick={() => navigate('/list-skill')}
                      >
                        List your first skill
                      </Button>
                    }
                  />
                ) : (
                  <div className="myagents__list">
                    {agents.map(agent => (
                      <AgentRow
                        key={agent.agent_id}
                        agent={agent}
                        earnings={earningsMap[agent.agent_id] ?? null}
                        apiKey={apiKey}
                        onNavigate={() => navigate(`/agents/${agent.agent_id}`)}
                        onRefresh={load}
                      />
                    ))}
                  </div>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {agents.length > 0 && (
            <Reveal delay={0.1}>
              <div className="myagents__hint">
                <ExternalLink size={12} />
                Click an agent row to see its public listing, job history, and trust score.
              </div>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
