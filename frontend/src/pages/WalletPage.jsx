import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import SpendChart from '../features/analytics/SpendChart'
import { createTopupSession, depositToWallet, fetchPublicConfig, fetchAgentEarnings, connectOnboard, getConnectStatus, withdrawFunds, fetchWithdrawals } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowDownLeft, ArrowUpRight, Plus, CreditCard, CheckCircle, X, TrendingUp, Bot, Banknote, ExternalLink, AlertCircle } from 'lucide-react'
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

function TxRow({ tx }) {
  const isCredit = CREDIT_TYPES.has(tx.type)
  const sign = isCredit ? '+' : '−'
  const color = isCredit ? 'var(--positive)' : 'var(--negative)'
  const Icon = isCredit ? ArrowDownLeft : ArrowUpRight

  return (
    <div className="wallet__tx-row">
      <div className="wallet__tx-icon" style={{ background: isCredit ? 'var(--positive-bg)' : 'var(--negative-bg)', border: `1px solid ${isCredit ? 'var(--positive-border)' : 'var(--negative-border)'}` }}>
        <Icon size={13} color={color} />
      </div>
      <div>
        <p className="wallet__tx-memo">{tx.memo || tx.type}</p>
        <p className="wallet__tx-date">{fmtDate(tx.created_at)}</p>
      </div>
      <Badge label={tx.type} />
      <span className="wallet__tx-amount t-mono" style={{ color }}>
        {sign}{fmtUsd(Math.abs(tx.amount_cents))}
      </span>
    </div>
  )
}

function AgentEarningsRow({ row }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 'var(--sp-3)',
      padding: 'var(--sp-3) 0',
      borderBottom: '1px solid var(--line-soft)',
    }}>
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        background: 'var(--accent-wash, #eef2ff)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        flexShrink: 0,
      }}>
        <Bot size={14} color="var(--accent)" />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ fontWeight: 600, fontSize: '0.875rem', color: 'var(--ink)', marginBottom: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {row.agent_name}
        </p>
        <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)' }}>
          {row.call_count} call{row.call_count !== 1 ? 's' : ''} · last {row.last_earned_at ? new Date(row.last_earned_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—'}
        </p>
      </div>
      <span style={{ fontWeight: 700, fontSize: '0.9375rem', color: 'var(--positive)', fontVariantNumeric: 'tabular-nums' }}>
        +{fmtUsd(row.total_earned_cents)}
      </span>
    </div>
  )
}

function WithdrawalRow({ item }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 'var(--sp-3)',
      padding: 'var(--sp-3) 0',
      borderBottom: '1px solid var(--line-soft)',
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ fontWeight: 600, fontSize: '0.875rem', color: 'var(--ink)', marginBottom: 2 }}>
          {item.memo || 'Withdrawal'}
        </p>
        <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)' }}>
          {fmtDate(item.created_at)} · {item.stripe_tx_id ? `Stripe ${String(item.stripe_tx_id).slice(0, 14)}…` : 'No Stripe ID'}
        </p>
      </div>
      <Badge label={item.status || 'complete'} />
      <span style={{ fontWeight: 700, fontSize: '0.875rem', color: 'var(--ink)', fontVariantNumeric: 'tabular-nums' }}>
        {fmtUsd(item.amount_cents)}
      </span>
    </div>
  )
}

export default function WalletPage() {
  const { wallet, apiKey, refreshWallet, showToast } = useMarket()
  const [searchParams, setSearchParams] = useSearchParams()
  const [amount, setAmount] = useState('10')
  const [stripeLoading, setStripeLoading] = useState(false)
  const [demoLoading, setDemoLoading] = useState(false)
  const [stripeEnabled, setStripeEnabled] = useState(false)
  const [paymentBanner, setPaymentBanner] = useState(null) // 'success' | 'cancelled' | null
  const [agentEarnings, setAgentEarnings] = useState(null) // null = loading, [] = empty
  const [connectStatus, setConnectStatus] = useState(null) // null = loading
  const [connectLoading, setConnectLoading] = useState(false)
  const [withdrawAmount, setWithdrawAmount] = useState('10')
  const [withdrawLoading, setWithdrawLoading] = useState(false)
  const [withdrawalHistory, setWithdrawalHistory] = useState(null)

  const transactions = wallet?.transactions ?? []
  const lowBalance = (wallet?.balance_cents ?? 0) < 500

  // Check Stripe availability on mount
  useEffect(() => {
    fetchPublicConfig()
      .then(cfg => setStripeEnabled(!!cfg?.stripe_enabled))
      .catch(() => {})
  }, [])

  // Fetch per-agent earnings
  useEffect(() => {
    if (!apiKey) return
    fetchAgentEarnings(apiKey)
      .then(data => setAgentEarnings(data?.earnings ?? []))
      .catch(() => setAgentEarnings([]))
  }, [apiKey])

  // Fetch Stripe Connect status
  useEffect(() => {
    if (!apiKey) return
    getConnectStatus(apiKey)
      .then(data => setConnectStatus(data))
      .catch(() => setConnectStatus({ connected: false, charges_enabled: false, account_id: null }))
  }, [apiKey])

  useEffect(() => {
    if (!apiKey) return
    fetchWithdrawals(apiKey, 10)
      .then(data => setWithdrawalHistory(data?.withdrawals ?? []))
      .catch(() => setWithdrawalHistory([]))
  }, [apiKey])

  // Handle Stripe redirect-back query params
  useEffect(() => {
    const payment = searchParams.get('payment')
    if (payment === 'success') {
      setPaymentBanner('success')
      // Poll wallet a few times to catch the webhook credit
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
      // Refetch connect status after returning from Stripe onboarding
      if (apiKey) {
        getConnectStatus(apiKey)
          .then(data => setConnectStatus(data))
          .catch(() => {})
      }
      setSearchParams({}, { replace: true })
    }
  }, []) // eslint-disable-line

  const handleStripeTopup = async (e) => {
    e.preventDefault()
    if (!wallet?.wallet_id) return
    const cents = Math.round(Number(amount) * 100)
    if (!Number.isFinite(cents) || cents < 100) {
      showToast?.('Minimum top-up is $1.00.', 'error')
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
    if (!Number.isFinite(cents) || cents < 100) {
      showToast?.('Minimum withdrawal is $1.00.', 'error')
      return
    }
    setWithdrawLoading(true)
    try {
      await withdrawFunds(apiKey, cents)
      await refreshWallet?.()
      const history = await fetchWithdrawals(apiKey, 10)
      setWithdrawalHistory(history?.withdrawals ?? [])
      showToast?.(`Withdrawal of $${(cents / 100).toFixed(2)} initiated.`, 'success')
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
    if (!Number.isFinite(cents) || cents <= 0) {
      showToast?.('Enter a valid amount.', 'error')
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

  return (
    <main className="wallet">
      <Topbar crumbs={[{ label: 'Wallet' }]} />

      <div className="wallet__scroll">
        <div className="wallet__content">

          {/* Payment result banner */}
          {paymentBanner === 'success' && (
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              gap: 'var(--sp-4)',
              padding: 'var(--sp-4) var(--sp-5)',
              background: 'var(--positive-wash)',
              border: '1px solid var(--positive-line)',
              borderRadius: 'var(--r-md)',
              marginBottom: 'var(--sp-5)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)' }}>
                <CheckCircle size={18} color="var(--positive)" />
                <div>
                  <p style={{ fontWeight: 600, fontSize: '0.9375rem', color: 'var(--positive)', marginBottom: 2 }}>
                    Payment received
                  </p>
                  <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)' }}>
                    Your balance will update within a few seconds as we confirm with Stripe.
                  </p>
                </div>
              </div>
              <button onClick={() => setPaymentBanner(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--ink-mute)' }}>
                <X size={16} />
              </button>
            </div>
          )}

          {paymentBanner === 'cancelled' && (
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              gap: 'var(--sp-4)',
              padding: 'var(--sp-4) var(--sp-5)',
              background: 'var(--warn-wash, #fffbe6)',
              border: '1px solid var(--warn-line, #f0d060)',
              borderRadius: 'var(--r-md)',
              marginBottom: 'var(--sp-5)',
            }}>
              <p style={{ fontSize: '0.875rem', color: 'var(--ink-soft)' }}>
                Payment cancelled — your balance was not changed.
              </p>
              <button onClick={() => setPaymentBanner(null)} style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--ink-mute)' }}>
                <X size={16} />
              </button>
            </div>
          )}

          <header className="wallet__header">
            <div>
              <p className="wallet__eyebrow">Settlement + trust</p>
              <h1>Wallet</h1>
              <p>All charges, refunds, and payouts are visible here. Keep enough balance for uninterrupted calls.</p>
            </div>
            <div>
              <p className="wallet__balance">{fmtUsd(wallet?.balance_cents)}</p>
              {wallet?.wallet_id && <p className="wallet__wallet-id">{wallet.wallet_id}</p>}
            </div>
          </header>

          <section className={`wallet__trust ${lowBalance ? 'wallet__trust--warn' : ''}`}>
            <p>
              {lowBalance
                ? 'Balance is low. Add funds to avoid failed pre-call charges.'
                : 'Healthy balance. Calls can be charged and settled automatically.'}
            </p>
            <div className="wallet__trust-badges">
              <Badge label="Charge before run" dot />
              <Badge label="Auto payout to agent" dot />
              <Badge label="Refund on failure" dot />
            </div>
          </section>

          <div className="wallet__grid">
            <Reveal>
            <Card>
              <Card.Header>
                <span className="wallet__section-title">Add funds</span>
              </Card.Header>
              <Card.Body>
                {/* Amount picker shared by both paths */}
                <div style={{ marginBottom: 'var(--sp-4)' }}>
                  <Input
                    label="Amount (USD)"
                    type="number"
                    min="1"
                    max="500"
                    step="1"
                    value={amount}
                    onChange={e => setAmount(e.target.value)}
                    required
                    mono
                  />
                  <div className="wallet__quick-amounts" style={{ marginTop: 'var(--sp-2)' }}>
                    {['5', '10', '25', '100'].map(v => (
                      <button
                        key={v}
                        type="button"
                        onClick={() => setAmount(v)}
                        className={amount === v ? 'wallet__quick-btn wallet__quick-btn--active' : 'wallet__quick-btn'}
                      >
                        ${v}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Stripe path (shown when configured) */}
                {stripeEnabled && (
                  <form onSubmit={handleStripeTopup} style={{ marginBottom: 'var(--sp-3)' }}>
                    <Button
                      type="submit"
                      variant="primary"
                      loading={stripeLoading}
                      icon={<CreditCard size={14} />}
                      style={{ width: '100%' }}
                    >
                      Pay {fmtUsd(Math.round((Number(amount) || 0) * 100))} with card
                    </Button>
                    <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', marginTop: 'var(--sp-2)', textAlign: 'center' }}>
                      Secured by Stripe. Funds appear within seconds after payment.
                    </p>
                  </form>
                )}

                {/* Demo deposit (shown when Stripe is not configured, or as secondary) */}
                {!stripeEnabled && (
                  <form onSubmit={handleDemoDeposit}>
                    <Button
                      type="submit"
                      variant={stripeEnabled ? 'secondary' : 'primary'}
                      loading={demoLoading}
                      icon={<Plus size={14} />}
                      style={{ width: '100%' }}
                    >
                      Add {fmtUsd(Math.round((Number(amount) || 0) * 100))}
                    </Button>
                    <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', marginTop: 'var(--sp-2)', textAlign: 'center' }}>
                      Demo mode — funds are credited instantly, no real payment.
                    </p>
                  </form>
                )}
              </Card.Body>
            </Card>
            </Reveal>

            <div className="wallet__right-col">
              {transactions.length > 0 && (
                <Reveal delay={0.1}>
                <Card>
                  <Card.Header>
                    <span className="wallet__section-title">14-day spend</span>
                  </Card.Header>
                  <Card.Body>
                    <SpendChart transactions={transactions} />
                  </Card.Body>
                </Card>
                </Reveal>
              )}

              {/* Agent earnings breakdown */}
              <Reveal delay={0.15}>
              <Card>
                <Card.Header style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)' }}>
                  <TrendingUp size={14} color="var(--accent)" />
                  <span className="wallet__section-title">
                    Agent earnings
                    {agentEarnings && agentEarnings.length > 0 && (
                      <span style={{ marginLeft: 'var(--sp-2)', color: 'var(--ink-mute)', fontWeight: 400 }}>
                        · {fmtUsd(agentEarnings.reduce((s, r) => s + r.total_earned_cents, 0))} total
                      </span>
                    )}
                  </span>
                </Card.Header>
                <Card.Body>
                  {agentEarnings === null ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)', padding: 'var(--sp-2) 0' }}>
                      {[1,2,3].map(i => <Skeleton key={i} variant="rect" height={44} />)}
                    </div>
                  ) : agentEarnings.length === 0 ? (
                    <EmptyState
                      title="No agent earnings yet"
                      sub="When agents you've listed get called, your earnings appear here per agent."
                    />
                  ) : (
                    <div>
                      {agentEarnings.map((row, i) => (
                        <AgentEarningsRow key={row.agent_id ?? i} row={row} />
                      ))}
                    </div>
                  )}
                </Card.Body>
              </Card>
              </Reveal>

              {/* Stripe Connect / Withdraw card */}
              {stripeEnabled && (
                <Reveal delay={0.2}>
                <Card>
                  <Card.Header style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)' }}>
                    <Banknote size={14} color="var(--accent)" />
                    <span className="wallet__section-title">Withdraw earnings</span>
                  </Card.Header>
                  <Card.Body>
                    {connectStatus === null ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)', padding: 'var(--sp-2) 0' }}>
                        <Skeleton variant="rect" height={40} />
                        <Skeleton variant="text" width="60%" />
                      </div>
                    ) : connectStatus.unavailable ? (
                      <p style={{ fontSize: '0.8125rem', color: 'var(--ink-mute)' }}>Withdrawals not available on this server.</p>
                    ) : !connectStatus.connected ? (
                      <div>
                        <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', marginBottom: 'var(--sp-4)' }}>
                          Connect a bank account to withdraw your agent earnings to cash.
                          Powered by Stripe Connect — secure and instant.
                        </p>
                        <Button
                          variant="primary"
                          loading={connectLoading}
                          icon={<ExternalLink size={13} />}
                          style={{ width: '100%' }}
                          onClick={handleConnectOnboard}
                        >
                          Connect bank account
                        </Button>
                      </div>
                    ) : !connectStatus.charges_enabled ? (
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginBottom: 'var(--sp-3)' }}>
                          <AlertCircle size={14} color="var(--warn, #d97706)" />
                          <p style={{ fontSize: '0.8125rem', color: 'var(--warn, #d97706)', fontWeight: 600 }}>Onboarding incomplete</p>
                        </div>
                        <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)', marginBottom: 'var(--sp-4)' }}>
                          Your Stripe account is connected but not yet approved for payouts. Finish the onboarding steps.
                        </p>
                        <Button
                          variant="secondary"
                          loading={connectLoading}
                          icon={<ExternalLink size={13} />}
                          style={{ width: '100%' }}
                          onClick={handleConnectOnboard}
                        >
                          Resume onboarding
                        </Button>
                      </div>
                    ) : (
                      <form onSubmit={handleWithdraw}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginBottom: 'var(--sp-3)' }}>
                          <CheckCircle size={14} color="var(--positive)" />
                          <p style={{ fontSize: '0.8125rem', color: 'var(--positive)', fontWeight: 600 }}>Bank account connected</p>
                        </div>
                        {/* Fee breakdown */}
                        {(() => {
                          const gross = Math.round((Number(withdrawAmount) || 0) * 100)
                          const stripeFee = Math.round(gross * 0.0025) + 25 // ~0.25% + $0.25
                          const net = Math.max(0, gross - stripeFee)
                          return gross >= 100 ? (
                            <div style={{ background: 'var(--surface-alt, #f7f8fa)', borderRadius: 'var(--r-sm)', padding: 'var(--sp-3)', marginBottom: 'var(--sp-3)', fontSize: '0.8125rem' }}>
                              <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--ink-soft)', marginBottom: 4 }}>
                                <span>Withdrawal amount</span><span>{fmtUsd(gross)}</span>
                              </div>
                              <div style={{ display: 'flex', justifyContent: 'space-between', color: 'var(--ink-mute)', marginBottom: 4 }}>
                                <span>Stripe fee (~0.25% + $0.25)</span><span>−{fmtUsd(stripeFee)}</span>
                              </div>
                              <div style={{ display: 'flex', justifyContent: 'space-between', fontWeight: 700, color: 'var(--ink)', borderTop: '1px solid var(--line-soft)', paddingTop: 4 }}>
                                <span>You receive</span><span>{fmtUsd(net)}</span>
                              </div>
                            </div>
                          ) : null
                        })()}
                        <Input
                          label="Withdraw amount (USD)"
                          type="number"
                          min="1"
                          max={((wallet?.balance_cents ?? 0) / 100).toFixed(2)}
                          step="1"
                          value={withdrawAmount}
                          onChange={e => setWithdrawAmount(e.target.value)}
                          required
                          mono
                          hint={`Available: ${fmtUsd(wallet?.balance_cents ?? 0)} · Platform fee already deducted from earnings`}
                          style={{ marginBottom: 'var(--sp-3)' }}
                        />
                        <Button
                          type="submit"
                          variant="primary"
                          loading={withdrawLoading}
                          icon={<Banknote size={14} />}
                          style={{ width: '100%' }}
                        >
                          Withdraw {fmtUsd(Math.round((Number(withdrawAmount) || 0) * 100))}
                        </Button>
                      </form>
                    )}
                  </Card.Body>
                </Card>
                </Reveal>
              )}

              {stripeEnabled && (
                <Reveal delay={0.23}>
                <Card>
                  <Card.Header>
                    <span className="wallet__section-title">Withdrawal history</span>
                  </Card.Header>
                  <Card.Body>
                    {withdrawalHistory === null ? (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)', padding: 'var(--sp-2) 0' }}>
                        {[1,2].map(i => <Skeleton key={i} variant="rect" height={44} />)}
                      </div>
                    ) : withdrawalHistory.length === 0 ? (
                      <EmptyState
                        title="No withdrawals yet"
                        sub="Completed payout transfers will appear here."
                      />
                    ) : (
                      <div>
                        {withdrawalHistory.map((item, idx) => (
                          <WithdrawalRow key={item.transfer_id ?? idx} item={item} />
                        ))}
                      </div>
                    )}
                  </Card.Body>
                </Card>
                </Reveal>
              )}

              <Reveal delay={0.25}>
              <Card>
                <Card.Header>
                  <span className="wallet__section-title">
                    Transactions {transactions.length > 0 && `(${transactions.length})`}
                  </span>
                </Card.Header>
                <Card.Body>
                  {transactions.length === 0 ? (
                    <EmptyState title="No transactions yet" sub="Deposits, charges, payouts, and refunds appear here." />
                  ) : (
                    <div>
                      {transactions.map((tx, i) => (
                        <TxRow key={tx.tx_id ?? i} tx={tx} />
                      ))}
                    </div>
                  )}
                </Card.Body>
              </Card>
              </Reveal>
            </div>
          </div>
        </div>
      </div>
    </main>
  )
}
