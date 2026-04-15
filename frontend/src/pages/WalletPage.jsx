import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Input from '../ui/Input'
import SpendChart from '../features/analytics/SpendChart'
import { createTopupSession, depositToWallet, fetchPublicConfig } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowDownLeft, ArrowUpRight, Plus, CreditCard, CheckCircle, X } from 'lucide-react'
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
      <div className="wallet__tx-icon" style={{ background: isCredit ? 'var(--positive-wash)' : 'var(--negative-wash)' }}>
        <Icon size={14} color={color} />
      </div>
      <div>
        <p className="wallet__tx-memo">{tx.memo || tx.type}</p>
        <p className="wallet__tx-date">{fmtDate(tx.created_at)}</p>
      </div>
      <Badge label={tx.type} />
      <span className="wallet__tx-amount" style={{ color }}>
        {sign}{fmtUsd(Math.abs(tx.amount_cents))}
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

  const transactions = wallet?.transactions ?? []
  const lowBalance = (wallet?.balance_cents ?? 0) < 500

  // Check Stripe availability on mount
  useEffect(() => {
    fetchPublicConfig()
      .then(cfg => setStripeEnabled(!!cfg?.stripe_enabled))
      .catch(() => {})
  }, [])

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

            <div className="wallet__right-col">
              {transactions.length > 0 && (
                <Card>
                  <Card.Header>
                    <span className="wallet__section-title">14-day spend</span>
                  </Card.Header>
                  <Card.Body>
                    <SpendChart transactions={transactions} />
                  </Card.Body>
                </Card>
              )}

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
            </div>
          </div>
        </div>
      </div>
    </main>
  )
}
