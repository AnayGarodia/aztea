import { useEffect, useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import Skeleton from '../ui/Skeleton'
import { fetchAdminPlatformEarnings, adminPlatformWithdraw } from '../api'
import { useMarket } from '../context/MarketContext'
import { Coins, ArrowDownToLine } from 'lucide-react'
import './AdminEarningsPage.css'
import { fmtDate, fmtUsd } from '../utils/format.js'

const POOLS = [
  { key: 'platform', label: 'Platform fees (10%)', hint: 'Accumulated from every agent call on the platform.' },
  { key: 'system_agents', label: 'Built-in agents payouts (90%)', hint: 'Earnings from built-in agents owned by the system account.' },
]

function txTypeLabel(tx) {
  const memo = String(tx?.memo || '')
  if (memo.startsWith('[admin-transfer] ')) {
    return Number(tx?.amount_cents) >= 0 ? 'admin_deposit' : 'admin_withdraw'
  }
  return String(tx?.type || 'unknown')
}

export default function AdminEarningsPage() {
  const { apiKey, showToast } = useMarket()
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [withdrawAmount, setWithdrawAmount] = useState({})
  const [withdrawing, setWithdrawing] = useState(null)

  const refresh = async () => {
    setError(null)
    try {
      const body = await fetchAdminPlatformEarnings(apiKey)
      setData(body)
    } catch (err) {
      setError(err?.message ?? 'Failed to load platform earnings.')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { refresh() }, [apiKey]) // eslint-disable-line

  const onWithdraw = async (sourceKey) => {
    const amtStr = String(withdrawAmount[sourceKey] ?? '').trim()
    const dollars = parseFloat(amtStr)
    if (!Number.isFinite(dollars) || dollars <= 0) {
      showToast?.('Enter a positive USD amount.', 'error')
      return
    }
    const cents = Math.round(dollars * 100)
    setWithdrawing(sourceKey)
    try {
      const res = await adminPlatformWithdraw(apiKey, { source: sourceKey, amount_cents: cents })
      showToast?.(`Transferred ${fmtUsd(res.transferred_cents)} to your wallet.`, 'success')
      setWithdrawAmount(prev => ({ ...prev, [sourceKey]: '' }))
      await refresh()
    } catch (err) {
      showToast?.(err?.message ?? 'Withdrawal failed.', 'error')
    } finally {
      setWithdrawing(null)
    }
  }

  return (
    <main className="admin-earnings">
      <Topbar crumbs={[{ label: 'Platform Earnings' }]} />
      <div className="admin-earnings__scroll">
        <div className="admin-earnings__content">
          <Reveal>
            <header className="admin-earnings__header">
              <div>
                <p className="admin-earnings__eyebrow t-micro">Admin only</p>
                <h1>Platform earnings</h1>
                <p>
                  Two pools accumulate revenue: the 10% platform fee on every agent call,
                  and the 90% payout from built-in agents owned by the system account.
                  Withdraw either pool into your own wallet to cash out via Stripe Connect.
                </p>
              </div>
              <Button variant="ghost" size="sm" onClick={refresh} disabled={loading}>Refresh</Button>
            </header>
          </Reveal>

          {loading ? (
            <div className="admin-earnings__grid">
              <Skeleton variant="rect" height={220} />
              <Skeleton variant="rect" height={220} />
            </div>
          ) : error ? (
            <Card>
              <Card.Body>
                <p className="admin-earnings__error">{error}</p>
                <Button variant="ghost" size="sm" onClick={refresh}>Retry</Button>
              </Card.Body>
            </Card>
          ) : (
            <div className="admin-earnings__grid">
              {POOLS.map(pool => {
                const bucket = data?.[pool.key]
                const balance = Number(bucket?.balance_cents || 0)
                const txs = bucket?.recent_transactions || []
                return (
                  <Reveal key={pool.key}>
                    <Card>
                      <Card.Header>
                        <div className="admin-earnings__pool-head">
                          <Coins size={14} />
                          <span>{pool.label}</span>
                        </div>
                      </Card.Header>
                      <Card.Body>
                        <p className="admin-earnings__hint">{pool.hint}</p>
                        <div className="admin-earnings__balance">
                          <span className="admin-earnings__balance-label">Balance</span>
                          <span className="admin-earnings__balance-amount">{fmtUsd(balance)}</span>
                        </div>

                        <form
                          className="admin-earnings__withdraw"
                          onSubmit={(e) => { e.preventDefault(); onWithdraw(pool.key) }}
                        >
                          <Input
                            label="Amount (USD)"
                            type="number"
                            min="0"
                            step="0.01"
                            placeholder="0.00"
                            value={withdrawAmount[pool.key] ?? ''}
                            onChange={e => setWithdrawAmount(prev => ({ ...prev, [pool.key]: e.target.value }))}
                          />
                          <Button
                            type="submit"
                            variant="primary"
                            size="md"
                            loading={withdrawing === pool.key}
                            disabled={balance <= 0}
                            icon={<ArrowDownToLine size={13} />}
                          >
                            Withdraw to my wallet
                          </Button>
                        </form>

                        <p className="admin-earnings__section-label">Recent transactions</p>
                        {txs.length === 0 ? (
                          <p className="admin-earnings__empty">No transactions yet.</p>
                        ) : (
                          <div className="admin-earnings__tx-list">
                            {txs.slice(0, 8).map(tx => (
                              <div key={tx.tx_id} className="admin-earnings__tx">
                                <div>
                                  <span className={`admin-earnings__tx-type admin-earnings__tx-type--${txTypeLabel(tx)}`}>{txTypeLabel(tx)}</span>
                                  <span className="admin-earnings__tx-memo">{tx.memo || '—'}</span>
                                </div>
                                <div className="admin-earnings__tx-right">
                                  <span className={`admin-earnings__tx-amt ${Number(tx.amount_cents) >= 0 ? 'pos' : 'neg'}`}>
                                    {Number(tx.amount_cents) >= 0 ? '+' : ''}{fmtUsd(tx.amount_cents)}
                                  </span>
                                  <span className="admin-earnings__tx-date">{fmtDate(tx.created_at)}</span>
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                      </Card.Body>
                    </Card>
                  </Reveal>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </main>
  )
}
