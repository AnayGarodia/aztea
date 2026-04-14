import { useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Input from '../ui/Input'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import { depositToWallet } from '../api'
import { useMarket } from '../context/MarketContext'
import { ArrowDownLeft, ArrowUpRight, Plus } from 'lucide-react'

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
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'auto 1fr auto auto',
      gap: 'var(--sp-3)',
      alignItems: 'center',
      padding: '12px 0',
      borderBottom: '1px solid var(--line)',
    }}>
      {/* Icon */}
      <div style={{
        width: 32, height: 32, borderRadius: '50%',
        background: isCredit ? 'var(--positive-wash)' : 'var(--negative-wash)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
      }}>
        <Icon size={14} color={color} />
      </div>

      {/* Description */}
      <div style={{ minWidth: 0 }}>
        <p style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--ink)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {tx.memo || tx.type}
        </p>
        <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)' }}>
          {fmtDate(tx.created_at)}
        </p>
      </div>

      {/* Type badge */}
      <Badge label={tx.type} />

      {/* Amount */}
      <span style={{
        fontFamily: 'var(--font-mono)',
        fontSize: '0.9375rem',
        fontWeight: 600,
        color,
        fontFeatureSettings: '"tnum"',
        whiteSpace: 'nowrap',
      }}>
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
      showToast?.('Funds added.', 'success')
      setAmount('10')
    } catch (err) {
      showToast?.(err?.message ?? 'Deposit failed.', 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ label: 'Wallet' }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Balance hero */}
        <div style={{ marginBottom: 'var(--sp-7)' }}>
          <p style={{
            fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.07em',
            textTransform: 'uppercase', color: 'var(--ink-mute)', marginBottom: 'var(--sp-2)',
          }}>
            Available balance
          </p>
          <p style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 'clamp(2.5rem, 6vw, 3.75rem)',
            fontWeight: 500,
            color: 'var(--ink)',
            letterSpacing: '-0.03em',
            fontFeatureSettings: '"tnum"',
            lineHeight: 1,
          }}>
            {fmtUsd(wallet?.balance_cents)}
          </p>
          {wallet?.wallet_id && (
            <p style={{ marginTop: 8, fontSize: '0.75rem', color: 'var(--ink-mute)', fontFamily: 'var(--font-mono)' }}>
              {wallet.wallet_id}
            </p>
          )}
        </div>

        <div style={{ display: 'grid', gap: 'var(--sp-5)', gridTemplateColumns: '320px 1fr', alignItems: 'start' }}>

          {/* Add funds */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Add funds</span>
            </Card.Header>
            <Card.Body>
              <form onSubmit={handleDeposit} style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
                <div>
                  <p style={{ fontSize: '0.8125rem', fontWeight: 500, color: 'var(--ink-soft)', marginBottom: 'var(--sp-1)' }}>
                    Amount (USD)
                  </p>
                  <div style={{ position: 'relative' }}>
                    <span style={{
                      position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)',
                      color: 'var(--ink-mute)', fontSize: '0.875rem', pointerEvents: 'none',
                    }}>
                      $
                    </span>
                    <input
                      type="number"
                      min="0.01"
                      step="0.01"
                      value={amount}
                      onChange={e => setAmount(e.target.value)}
                      required
                      style={{
                        width: '100%', height: 42, paddingLeft: 26, paddingRight: 'var(--sp-3)',
                        fontFamily: 'var(--font-mono)', fontSize: '1.125rem', fontWeight: 500,
                        color: 'var(--ink)', background: 'var(--surface)',
                        border: '1px solid var(--line)', borderRadius: 'var(--r-sm)',
                        outline: 'none', fontFeatureSettings: '"tnum"',
                        transition: 'border-color var(--duration-sm)',
                      }}
                      onFocus={e => { e.target.style.borderColor = 'var(--accent-line)'; e.target.style.boxShadow = 'var(--focus-ring)' }}
                      onBlur={e => { e.target.style.borderColor = 'var(--line)'; e.target.style.boxShadow = 'none' }}
                    />
                  </div>
                </div>

                {/* Quick amounts */}
                <div style={{ display: 'flex', gap: 'var(--sp-2)' }}>
                  {['5', '10', '25', '100'].map(v => (
                    <button
                      key={v}
                      type="button"
                      onClick={() => setAmount(v)}
                      style={{
                        flex: 1, padding: '6px 0', fontSize: '0.8125rem', fontWeight: 500,
                        color: amount === v ? 'var(--accent)' : 'var(--ink-soft)',
                        background: amount === v ? 'var(--accent-wash)' : 'var(--canvas-sunk)',
                        border: '1px solid',
                        borderColor: amount === v ? 'var(--accent-line)' : 'var(--line)',
                        borderRadius: 'var(--r-sm)',
                        cursor: 'pointer',
                        transition: 'all var(--duration-sm) var(--ease)',
                        fontFamily: 'var(--font-mono)',
                        fontFeatureSettings: '"tnum"',
                      }}
                    >
                      ${v}
                    </button>
                  ))}
                </div>

                <Button type="submit" variant="primary" loading={loading} icon={<Plus size={14} />}>
                  Add ${amount || '0'}
                </Button>

                <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', lineHeight: 1.5 }}>
                  In production, this connects to a payment processor. Funds are available immediately for agent calls.
                </p>
              </form>
            </Card.Body>
          </Card>

          {/* Transaction history */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>
                Transactions {transactions.length > 0 && `(${transactions.length})`}
              </span>
            </Card.Header>
            <Card.Body>
              {transactions.length === 0 ? (
                <EmptyState title="No transactions" sub="Deposits and charges will appear here." />
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
    </main>
  )
}
