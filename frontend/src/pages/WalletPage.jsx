import { useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Input from '../ui/Input'
import Button from '../ui/Button'
import { depositToWallet } from '../api'
import { useMarket } from '../context/MarketContext'

function fmtUsd(cents) {
  if (typeof cents !== 'number') return '--'
  return `$${(cents / 100).toFixed(2)}`
}

export default function WalletPage() {
  const { wallet, apiKey, refreshWallet, showToast } = useMarket()
  const [amount, setAmount] = useState('10')
  const [loading, setLoading] = useState(false)

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
      await depositToWallet(apiKey, wallet.wallet_id, cents, 'dashboard deposit')
      await refreshWallet?.()
      showToast?.('Deposit posted.', 'success')
    } catch (err) {
      showToast?.(err?.message ?? 'Deposit failed', 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ label: 'Wallet' }]} />
      <Card>
        <Card.Header>
          <strong>Balance</strong>
        </Card.Header>
        <Card.Body>
          <p style={{ margin: 0, fontSize: 28 }}>{fmtUsd(wallet?.balance_cents)}</p>
        </Card.Body>
      </Card>
      <Card>
        <Card.Header>
          <strong>Add funds</strong>
        </Card.Header>
        <Card.Body>
          <form onSubmit={handleDeposit} style={{ display: 'flex', alignItems: 'end', gap: 10 }}>
            <Input
              label="Amount (USD)"
              type="number"
              min="0.01"
              step="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              required
            />
            <Button type="submit" loading={loading}>Deposit</Button>
          </form>
        </Card.Body>
      </Card>
    </main>
  )
}
