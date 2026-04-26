import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import {
  createTopupSession,
  depositToWallet,
  fetchPublicConfig,
  fetchAgentWallets,
  fetchAgentEarnings,
  connectOnboard,
  getConnectStatus,
  withdrawFunds,
  fetchWithdrawals,
  fetchSpendSummary,
} from '../api'
import { useMarket } from '../context/MarketContext'
import {
  ArrowDownLeft,
  ArrowUpRight,
  Plus,
  CreditCard,
  CheckCircle,
  X,
  Bot,
  Banknote,
  ExternalLink,
  AlertCircle,
} from 'lucide-react'
import './WalletPage.css'

function fmtUsd(cents) {
  if (typeof cents !== 'number') return '--'
  return '$' + (cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtDate(str) {
  if (!str) return ''
  return new Date(str).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const CREDIT_TYPES = new Set(['deposit', 'refund', 'payout'])
const MIN_DEPOSIT_CENTS = 500
const ACTIVITY_PAGE_SIZE = 8

function ActivityRow({ entry }) {
  const isCredit = entry.kind === 'credit'
  const isWithdrawal = entry.kind === 'withdrawal'
  const sign = isCredit ? '+' : '−'
  const color = isCredit ? 'var(--positive)' : 'var(--negative)'
  const Icon = isWithdrawal ? Banknote : isCredit ? ArrowDownLeft : ArrowUpRight

  return (
    <div className="wallet__activity-row">
      <div
        className="wallet__activity-icon"
        style={{
          background: isCredit ? 'var(--positive-bg)' : 'var(--negative-bg)',
          border: `1px solid ${isCredit ? 'var(--positive-border)' : 'var(--negative-border)'}`,
        }}
      >
        <Icon size={13} color={color} />
      </div>
      <div className="wallet__activity-text">
        <p className="wallet__activity-memo">{entry.label}</p>
        <p className="wallet__activity-date">{fmtDate(entry.created_at)}</p>
      </div>
      {entry.badge && <Badge label={entry.badge} />}
      <span className="wallet__activity-amount t-mono" style={{ color }}>
        {sign}{fmtUsd(Math.abs(entry.amount_cents))}
      </span>
    </div>
  )
}

export default function WalletPage() {
  const { wallet, apiKey, refreshWallet, showToast, agents } = useMarket()
  const [searchParams, setSearchParams] = useSearchParams()
  const [amount, setAmount] = useState('10')
  const [stripeLoading, setStripeLoading] = useState(false)
  const [demoLoading, setDemoLoading] = useState(false)
  const [stripeEnabled, setStripeEnabled] = useState(false)
  const [paymentBanner, setPaymentBanner] = useState(null)
  const [agentEarnings, setAgentEarnings] = useState(null)
  const [connectStatus, setConnectStatus] = useState(null)
  const [connectLoading, setConnectLoading] = useState(false)
  const [withdrawAmount, setWithdrawAmount] = useState('10')
  const [withdrawLoading, setWithdrawLoading] = useState(false)
  const [withdrawalHistory, setWithdrawalHistory] = useState(null)
  const [spendPeriod, setSpendPeriod] = useState('7d')
  const [spendSummary, setSpendSummary] = useState(null)
  const [spendLoading, setSpendLoading] = useState(false)
  const [actionTab, setActionTab] = useState('add') // 'add' | 'withdraw'
  const [activityVisible, setActivityVisible] = useState(ACTIVITY_PAGE_SIZE)

  const transactions = wallet?.transactions ?? []
  const lowBalance = (wallet?.balance_cents ?? 0) < 500

  useEffect(() => {
    fetchPublicConfig().then(cfg => setStripeEnabled(!!cfg?.stripe_enabled)).catch(() => {})
  }, [])

  useEffect(() => {
    if (!apiKey) return
    fetchAgentWallets(apiKey)
      .then(data => setAgentEarnings(data?.agents ?? []))
      .catch(() => {
        fetchAgentEarnings(apiKey)
          .then(data => setAgentEarnings(data?.earnings ?? []))
          .catch(() => setAgentEarnings([]))
      })
  }, [apiKey])

  useEffect(() => {
    if (!apiKey) return
    getConnectStatus(apiKey)
      .then(data => setConnectStatus(data))
      .catch(() => setConnectStatus({ connected: false, charges_enabled: false, account_id: null }))
  }, [apiKey])

  useEffect(() => {
    if (!apiKey) return
    fetchWithdrawals(apiKey, 50)
      .then(data => setWithdrawalHistory(data?.withdrawals ?? []))
      .catch(() => setWithdrawalHistory([]))
  }, [apiKey])

  useEffect(() => {
    if (!apiKey) return
    setSpendLoading(true)
    fetchSpendSummary(apiKey, spendPeriod)
      .then(data => {
        if (!data) return setSpendSummary(null)
        const agentMap = Object.fromEntries((agents ?? []).map(a => [a.agent_id, a.name]))
        setSpendSummary({
          ...data,
          by_agent: (data.by_agent ?? []).map(row => ({
            ...row,
            agent_name: agentMap[row.agent_id] ?? row.agent_id,
          })),
        })
      })
      .catch(() => setSpendSummary(null))
      .finally(() => setSpendLoading(false))
  }, [apiKey, spendPeriod, agents])

  useEffect(() => {
    const payment = searchParams.get('payment')
    if (payment === 'success') {
      setPaymentBanner('success')
      let attempts = 0
      const poll = setInterval(() => {
        refreshWallet?.()
        attempts++
        if (attempts >= 6) clearInterval(poll)
      }, 2000)
      setSearchParams({}, { replace: true })
    } else if (payment === 'cancelled') {
      setPaymentBanner('cancelled')
      setSearchParams({}, { replace: true })
    }
    const connect = searchParams.get('connect')
    if (connect === 'success' || connect === 'refresh') {
      if (apiKey) {
        getConnectStatus(apiKey).then(data => setConnectStatus(data)).catch(() => {})
      }
      setSearchParams({}, { replace: true })
    }
  }, []) // eslint-disable-line

  // Reset paging when underlying data changes meaningfully
  useEffect(() => {
    setActivityVisible(ACTIVITY_PAGE_SIZE)
  }, [transactions.length, withdrawalHistory?.length])

  // Merged, sorted activity timeline (transactions + withdrawals)
  const activity = useMemo(() => {
    const items = []
    for (const tx of transactions) {
      items.push({
        id: `tx-${tx.tx_id}`,
        kind: CREDIT_TYPES.has(tx.type) ? 'credit' : 'debit',
        label: tx.memo || tx.type,
        badge: tx.type,
        amount_cents: tx.amount_cents,
        created_at: tx.created_at,
      })
    }
    for (const w of withdrawalHistory ?? []) {
      items.push({
        id: `wd-${w.transfer_id ?? w.created_at}`,
        kind: 'withdrawal',
        label: w.memo || 'Withdrawal',
        badge: w.status || 'withdrawal',
        amount_cents: -Math.abs(w.amount_cents ?? 0),
        created_at: w.created_at,
      })
    }
    items.sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
    return items
  }, [transactions, withdrawalHistory])

  const totalEarnedCents = (agentEarnings ?? []).reduce((s, r) => s + (r.total_earned_cents ?? 0), 0)
  const heldCents = (agentEarnings ?? []).reduce((s, r) => s + (r.current_balance_cents ?? 0), 0)
  const hasEarnings = (agentEarnings?.length ?? 0) > 0

  const handleStripeTopup = async (e) => {
    e.preventDefault()
    if (!wallet?.wallet_id) return
    const cents = Math.round(Number(amount) * 100)
    if (!Number.isFinite(cents) || cents < MIN_DEPOSIT_CENTS) {
      showToast?.(`Minimum top-up is ${fmtUsd(MIN_DEPOSIT_CENTS)}.`, 'error')
      return
    }
    if (cents > 50000) {
      showToast?.('Maximum top-up is $500.00.', 'error')
      return
    }
    setStripeLoading(true)
    try {
      const session = await createTopupSession(apiKey, wallet.wallet_id, cents)
      window.location.href = session.checkout_url
    } catch (err) {
      showToast?.(err?.message ?? 'Could not start payment session.', 'error')
    } finally {
      setStripeLoading(false)
    }
  }

  const handleConnectOnboard = async () => {
    setConnectLoading(true)
    try {
      const data = await connectOnboard(apiKey)
      if (data?.onboarding_url) window.location.href = data.onboarding_url
    } catch (err) {
      showToast?.(err?.message ?? 'Could not start Stripe onboarding.', 'error')
    } finally {
      setConnectLoading(false)
    }
  }

  const handleWithdraw = async (e) => {
    e.preventDefault()
    const cents = Math.round(Number(withdrawAmount) * 100)
    const available = wallet?.balance_cents ?? 0
    if (!Number.isFinite(cents) || cents < 100) {
      showToast?.('Minimum withdrawal is $1.00.', 'error')
      return
    }
    if (cents > available) {
      showToast?.(`You only have ${fmtUsd(available)} available.`, 'error')
      return
    }
    setWithdrawLoading(true)
    try {
      await withdrawFunds(apiKey, cents)
      await refreshWallet?.()
      const history = await fetchWithdrawals(apiKey, 50)
      setWithdrawalHistory(history?.withdrawals ?? [])
      showToast?.(`Withdrawal of ${fmtUsd(cents)} initiated.`, 'success')
      setWithdrawAmount('10')
    } catch (err) {
      showToast?.(err?.message ?? 'Withdrawal failed.', 'error')
    } finally {
      setWithdrawLoading(false)
    }
  }

  const handleDemoDeposit = async (e) => {
    e.preventDefault()
    if (!wallet?.wallet_id) return
    const cents = Math.round(Number(amount) * 100)
    if (!Number.isFinite(cents) || cents < MIN_DEPOSIT_CENTS) {
      showToast?.(`Minimum deposit is ${fmtUsd(MIN_DEPOSIT_CENTS)}.`, 'error')
      return
    }
    setDemoLoading(true)
    try {
      await depositToWallet(apiKey, wallet.wallet_id, cents, 'Manual deposit')
      await refreshWallet?.()
      showToast?.('Funds added to wallet.', 'success')
      setAmount('10')
    } catch (err) {
      showToast?.(err?.message ?? 'Deposit failed.', 'error')
    } finally {
      setDemoLoading(false)
    }
  }

  const showWithdrawTab = stripeEnabled && !connectStatus?.unavailable

  return (
    <main className="wallet">
      <Topbar crumbs={[{ label: 'Wallet' }]} />

      <div className="wallet__scroll">
        <div className="wallet__content">

          {paymentBanner === 'success' && (
            <div className="wallet__banner wallet__banner--ok">
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)' }}>
                <CheckCircle size={18} color="var(--positive)" />
                <div>
                  <p className="wallet__banner-title">Payment received</p>
                  <p className="wallet__banner-sub">Your balance updates within a few seconds.</p>
                </div>
              </div>
              <button onClick={() => setPaymentBanner(null)} className="wallet__banner-close"><X size={16} /></button>
            </div>
          )}
          {paymentBanner === 'cancelled' && (
            <div className="wallet__banner wallet__banner--warn">
              <p className="wallet__banner-sub">Payment cancelled — your balance was not changed.</p>
              <button onClick={() => setPaymentBanner(null)} className="wallet__banner-close"><X size={16} /></button>
            </div>
          )}

          {/* Hero: balance + tabbed action panel */}
          <Reveal>
          <section className="wallet__hero">
            <div className="wallet__hero-balance">
              <p className="wallet__hero-label">Available balance</p>
              <p className="wallet__balance">{fmtUsd(wallet?.balance_cents)}</p>
              {lowBalance && (
                <p className="wallet__low-warn">Low balance — add funds before your next call.</p>
              )}
              {hasEarnings && (
                <p className="wallet__hero-earned">
                  <span style={{ color: 'var(--positive)' }}>+{fmtUsd(totalEarnedCents)} earned</span>
                  {heldCents > 0 && <> · {fmtUsd(heldCents)} held in agent wallets</>}
                </p>
              )}
            </div>

            <div className="wallet__action-panel">
              <div className="wallet__action-tabs" role="tablist">
                <button
                  role="tab"
                  aria-selected={actionTab === 'add'}
                  className={`wallet__action-tab ${actionTab === 'add' ? 'is-active' : ''}`}
                  onClick={() => setActionTab('add')}
                >
                  <Plus size={13} /> Add funds
                </button>
                {showWithdrawTab && (
                  <button
                    role="tab"
                    aria-selected={actionTab === 'withdraw'}
                    className={`wallet__action-tab ${actionTab === 'withdraw' ? 'is-active' : ''}`}
                    onClick={() => setActionTab('withdraw')}
                  >
                    <Banknote size={13} /> Withdraw
                  </button>
                )}
              </div>

              {actionTab === 'add' && (
                <div className="wallet__action-body">
                  <Input
                    label="Amount (USD)"
                    type="number"
                    min={MIN_DEPOSIT_CENTS / 100}
                    max="500"
                    step="1"
                    value={amount}
                    onChange={e => setAmount(e.target.value)}
                    required
                    mono
                    hint={`Min ${fmtUsd(MIN_DEPOSIT_CENTS)} · Max $500.00`}
                  />
                  <div className="wallet__quick-amounts">
                    {['5', '10', '25', '100'].map(v => (
                      <button
                        key={v}
                        type="button"
                        onClick={() => setAmount(v)}
                        className={`wallet__quick-btn${amount === v ? ' wallet__quick-btn--active' : ''}`}
                      >${v}</button>
                    ))}
                  </div>
                  {stripeEnabled ? (
                    <form onSubmit={handleStripeTopup}>
                      <Button type="submit" variant="primary" loading={stripeLoading} icon={<CreditCard size={14} />} style={{ width: '100%' }}>
                        Pay {fmtUsd(Math.round((Number(amount) || 0) * 100))} with card
                      </Button>
                    </form>
                  ) : (
                    <form onSubmit={handleDemoDeposit}>
                      <Button type="submit" variant="primary" loading={demoLoading} icon={<Plus size={14} />} style={{ width: '100%' }}>
                        Add {fmtUsd(Math.round((Number(amount) || 0) * 100))}
                      </Button>
                      <p className="wallet__action-note">Demo mode — instant credit, no real payment.</p>
                    </form>
                  )}
                </div>
              )}

              {actionTab === 'withdraw' && (
                <div className="wallet__action-body">
                  {connectStatus === null ? (
                    <>
                      <Skeleton variant="rect" height={40} />
                      <Skeleton variant="text" width="60%" />
                    </>
                  ) : !connectStatus.connected ? (
                    <>
                      <p className="wallet__action-note" style={{ textAlign: 'left' }}>
                        Connect a bank account to withdraw earnings via Stripe.
                      </p>
                      <Button variant="primary" loading={connectLoading} icon={<ExternalLink size={13} />} style={{ width: '100%' }} onClick={handleConnectOnboard}>
                        Connect bank account
                      </Button>
                    </>
                  ) : !connectStatus.charges_enabled ? (
                    <>
                      <div className="wallet__inline-warn">
                        <AlertCircle size={14} />
                        <span>Onboarding incomplete — finish setup to enable payouts.</span>
                      </div>
                      <Button variant="secondary" loading={connectLoading} icon={<ExternalLink size={13} />} style={{ width: '100%' }} onClick={handleConnectOnboard}>
                        Resume onboarding
                      </Button>
                    </>
                  ) : (
                    <form onSubmit={handleWithdraw}>
                      <Input
                        label="Amount (USD)"
                        type="number"
                        min="1"
                        max={((wallet?.balance_cents ?? 0) / 100).toFixed(2)}
                        step="1"
                        value={withdrawAmount}
                        onChange={e => setWithdrawAmount(e.target.value)}
                        required
                        mono
                        hint={`Available: ${fmtUsd(wallet?.balance_cents ?? 0)}`}
                      />
                      {(() => {
                        const gross = Math.round((Number(withdrawAmount) || 0) * 100)
                        const fee = Math.round(gross * 0.0025) + 25
                        const net = Math.max(0, gross - fee)
                        if (gross < 100) return null
                        return (
                          <div className="wallet__fee-box">
                            <div><span>Withdraw</span><span>{fmtUsd(gross)}</span></div>
                            <div><span>Stripe fee (~0.25% + $0.25)</span><span>−{fmtUsd(fee)}</span></div>
                            <div className="wallet__fee-net"><span>You receive</span><span>{fmtUsd(net)}</span></div>
                          </div>
                        )
                      })()}
                      <Button type="submit" variant="primary" loading={withdrawLoading} icon={<Banknote size={14} />} style={{ width: '100%' }}>
                        Withdraw {fmtUsd(Math.round((Number(withdrawAmount) || 0) * 100))}
                      </Button>
                    </form>
                  )}
                </div>
              )}
            </div>
          </section>
          </Reveal>

          {/* Spending */}
          <Reveal delay={0.06}>
          <Card>
            <Card.Header className="wallet__card-head">
              <span className="wallet__section-title">Spending</span>
              <div className="wallet__period-tabs">
                {['1d', '7d', '30d', '90d'].map(p => (
                  <button key={p} type="button"
                    className={`wallet__period-btn${spendPeriod === p ? ' wallet__period-btn--active' : ''}`}
                    onClick={() => setSpendPeriod(p)}
                  >{p}</button>
                ))}
              </div>
            </Card.Header>
            <Card.Body>
              {spendLoading ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)' }}>
                  <Skeleton variant="rect" height={32} />
                  <Skeleton variant="rect" height={32} />
                </div>
              ) : !spendSummary || spendSummary.total_cents === 0 ? (
                <EmptyState title="No spend in this period" sub="Charges for agent calls will appear here." />
              ) : (
                <>
                  <div className="wallet__spend-totals">
                    <div className="wallet__spend-stat">
                      <span className="wallet__spend-stat-label">Spent</span>
                      <span className="wallet__spend-stat-value">{fmtUsd(spendSummary.total_cents)}</span>
                    </div>
                    <div className="wallet__spend-stat">
                      <span className="wallet__spend-stat-label">Jobs</span>
                      <span className="wallet__spend-stat-value">{spendSummary.total_jobs}</span>
                    </div>
                    <div className="wallet__spend-stat">
                      <span className="wallet__spend-stat-label">Avg / job</span>
                      <span className="wallet__spend-stat-value">
                        {spendSummary.total_jobs > 0 ? fmtUsd(Math.round(spendSummary.total_cents / spendSummary.total_jobs)) : '—'}
                      </span>
                    </div>
                  </div>
                  {spendSummary.by_agent?.length > 0 && (
                    <div className="wallet__spend-agents">
                      <div className="wallet__spend-agents-head">
                        <span>Agent</span><span>Jobs</span><span>Spent</span>
                      </div>
                      {spendSummary.by_agent.map((row, i) => (
                        <div key={row.agent_id ?? i} className="wallet__spend-agent-row">
                          <span className="wallet__spend-agent-name">{row.agent_name ?? row.agent_id}</span>
                          <span className="wallet__spend-agent-count">{row.job_count}</span>
                          <span className="wallet__spend-agent-total">{fmtUsd(row.total_cents)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </Card.Body>
          </Card>
          </Reveal>

          {/* Earnings (only when user has agent earnings) */}
          {hasEarnings && (
            <Reveal delay={0.1}>
            <Card>
              <Card.Header>
                <span className="wallet__section-title">Earnings by agent</span>
              </Card.Header>
              <Card.Body>
                <div className="wallet__earnings-list">
                  {agentEarnings.map((row, i) => (
                    <div key={row.agent_id ?? i} className="wallet__earnings-row">
                      <div className="wallet__earnings-icon"><Bot size={13} color="var(--accent)" /></div>
                      <div className="wallet__earnings-text">
                        <p className="wallet__earnings-name">{row.agent_name}</p>
                        <p className="wallet__earnings-meta">
                          {row.call_count} call{row.call_count !== 1 ? 's' : ''}
                          {typeof row.current_balance_cents === 'number' && <> · {fmtUsd(row.current_balance_cents)} held</>}
                        </p>
                      </div>
                      <span className="wallet__earnings-amount">+{fmtUsd(row.total_earned_cents)}</span>
                    </div>
                  ))}
                </div>
              </Card.Body>
            </Card>
            </Reveal>
          )}

          {/* Activity (merged, paginated) */}
          <Reveal delay={0.14}>
          <Card>
            <Card.Header className="wallet__card-head">
              <span className="wallet__section-title">
                Activity {activity.length > 0 && <span className="wallet__count">· {activity.length}</span>}
              </span>
            </Card.Header>
            <Card.Body>
              {activity.length === 0 ? (
                <EmptyState title="No activity yet" sub="Deposits, charges, payouts, and withdrawals appear here." />
              ) : (
                <>
                  <div>
                    {activity.slice(0, activityVisible).map(entry => (
                      <ActivityRow key={entry.id} entry={entry} />
                    ))}
                  </div>
                  {activityVisible < activity.length && (
                    <div className="wallet__load-more-wrap">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setActivityVisible(v => v + ACTIVITY_PAGE_SIZE)}
                      >
                        Show {Math.min(ACTIVITY_PAGE_SIZE, activity.length - activityVisible)} more
                      </Button>
                    </div>
                  )}
                </>
              )}
            </Card.Body>
          </Card>
          </Reveal>

        </div>
      </div>
    </main>
  )
}
