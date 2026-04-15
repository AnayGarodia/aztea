import { useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Input from '../ui/Input'
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
                        className={amount === v ? 'wallet__quick-btn wallet__quick-btn--active' : 'wallet__quick-btn'}
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
