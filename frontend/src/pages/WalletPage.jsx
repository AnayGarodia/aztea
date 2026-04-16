import { useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import SpendChart from '../features/analytics/SpendChart'
import { depositToWallet } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowDownLeft, ArrowUpRight, Plus } from 'lucide-react'
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

export default function WalletPage() {
  const { wallet, apiKey, refreshWallet, showToast } = useMarket()
  const [amount, setAmount] = useState('10')
  const [loading, setLoading] = useState(false)

  const transactions = wallet?.transactions ?? []
  const lowBalance = (wallet?.balance_cents ?? 0) < 500

  const handleDeposit = async (e) => {
    e.preventDefault()
    if (!wallet?.wallet_id) return
    const cents = Math.round(Number(amount) * 100)
    if (!Number.isFinite(cents) || cents <= 0) {
      showToast?.('Enter a valid amount.', 'error')
      return
    }
    setLoading(true)
    try {
      await depositToWallet(apiKey, wallet.wallet_id, cents, 'Dashboard deposit')
      await refreshWallet?.()
      showToast?.('Funds added to wallet.', 'success')
      setAmount('10')
    } catch (err) {
      showToast?.(err?.message ?? 'Deposit failed.', 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="wallet">
      <Topbar crumbs={[{ label: 'Wallet' }]} />

      <div className="wallet__scroll">
        <div className="wallet__content">

          {/* Hero balance */}
          <Reveal>
            <div className={`wallet__hero ${lowBalance ? 'wallet__hero--warn' : ''}`}>
              <div>
                <p className="wallet__eyebrow t-micro">Settlement & trust</p>
                <h1>Wallet</h1>
                <p className="wallet__hero-sub">All charges, refunds, and payouts in one auditable ledger.</p>
              </div>
              <div className="wallet__balance-wrap">
                <div className="wallet__balance-label t-micro">Available balance</div>
                <div className="wallet__balance t-mono">{fmtUsd(wallet?.balance_cents)}</div>
                {wallet?.wallet_id && <div className="wallet__wallet-id t-mono">{wallet.wallet_id}</div>}
                {lowBalance && <div className="wallet__low-warn">⚠ Low balance — add funds to continue</div>}
              </div>
            </div>
          </Reveal>

          {/* Status bar */}
          <div className="wallet__status-bar">
            <Badge label="deposit" dot />
            <Badge label="charge" dot />
            <Badge label="payout" dot />
            <Badge label="refund" dot />
            <span className="wallet__status-note">All transactions are insert-only and immutable.</span>
          </div>

          <div className="wallet__grid">
            {/* Deposit form */}
            <Reveal delay={0.1}>
              <Card>
                <Card.Header>
                  <span className="wallet__section-title">Add funds</span>
                </Card.Header>
                <Card.Body>
                  <form onSubmit={handleDeposit} className="wallet__deposit-form">
                    <Input
                      label="Amount (USD)"
                      type="number"
                      min="0.01"
                      step="0.01"
                      value={amount}
                      onChange={e => setAmount(e.target.value)}
                      required
                      mono
                      hint="Demo mode: funds are credited instantly."
                    />

                    <div className="wallet__quick-amounts">
                      {['5', '10', '25', '100'].map(v => (
                        <button
                          key={v}
                          type="button"
                          onClick={() => setAmount(v)}
                          className={`wallet__quick-btn ${amount === v ? 'wallet__quick-btn--active' : ''}`}
                        >
                          ${v}
                        </button>
                      ))}
                    </div>

                    <Button type="submit" variant="primary" loading={loading} icon={<Plus size={14} />}>
                      Add {fmtUsd(Math.round((Number(amount) || 0) * 100))}
                    </Button>
                  </form>
                </Card.Body>
              </Card>
            </Reveal>

            <div className="wallet__right-col">
              {transactions.length > 0 && (
                <Reveal delay={0.15}>
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

              <Reveal delay={0.2}>
                <Card>
                  <Card.Header>
                    <span className="wallet__section-title">
                      Transactions {transactions.length > 0 && `(${transactions.length})`}
                    </span>
                  </Card.Header>
                  <Card.Body>
                    {transactions.length === 0 ? (
                      <EmptyState agentId="empty-wallet" title="No transactions yet" sub="Deposits, charges, payouts, and refunds appear here." />
                    ) : (
                      <div>
                        {transactions.map((tx, i) => <TxRow key={tx.tx_id ?? i} tx={tx} />)}
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
