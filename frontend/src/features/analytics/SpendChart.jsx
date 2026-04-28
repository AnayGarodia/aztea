import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { fmtUsd } from '../../utils/format.js'

function buildDailySpend(transactions = []) {
  // Group transactions by date, sum charges
  const map = {}
  const now = new Date()

  // Initialize last 14 days
  for (let i = 13; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(now.getDate() - i)
    const key = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    map[key] = { date: key, spend: 0, balance: null }
  }

  // Fill in transaction data
  transactions.forEach(tx => {
    if (!tx.created_at) return
    const d = new Date(tx.created_at)
    const key = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    if (!map[key]) return
    if (tx.amount_cents < 0) {
      // Debit - this is spend
      map[key].spend += Math.abs(tx.amount_cents)
    }
  })

  return Object.values(map)
}

function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  return (
    <div style={{
      background: 'var(--surface)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--r-md)',
      padding: '10px 14px',
      boxShadow: 'var(--shadow-md)',
      fontSize: '0.8125rem',
    }}>
      <p style={{ fontWeight: 600, color: 'var(--text-primary)', marginBottom: 4 }}>{label}</p>
      <p style={{ color: 'var(--text-secondary)' }}>
        Spend: <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--negative)', fontWeight: 600 }}>
          {fmtUsd(payload[0]?.value)}
        </span>
      </p>
    </div>
  )
}

export default function SpendChart({ transactions = [] }) {
  const data = buildDailySpend(transactions)
  const hasData = data.some(d => d.spend > 0)

  if (!hasData) {
    return (
      <div style={{
        height: 180,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'var(--text-muted)',
        fontSize: '0.875rem',
        border: '1px dashed var(--border)',
        borderRadius: 'var(--r-md)',
      }}>
        Spend history will appear here
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={180}>
      <AreaChart data={data} margin={{ top: 8, right: 4, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="spendGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor="var(--negative)" stopOpacity={0.12} />
            <stop offset="95%" stopColor="var(--negative)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke="var(--border)" strokeDasharray="4 4" vertical={false} />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 10, fill: 'var(--text-muted)', fontFamily: 'Geist, sans-serif' }}
          axisLine={false}
          tickLine={false}
          interval={3}
        />
        <YAxis
          tick={{ fontSize: 10, fill: 'var(--text-muted)', fontFamily: 'Geist Mono, monospace' }}
          axisLine={false}
          tickLine={false}
          tickFormatter={v => v > 0 ? `$${(v / 100).toFixed(0)}` : ''}
          width={36}
        />
        <Tooltip content={<CustomTooltip />} cursor={{ stroke: 'var(--border-bright)', strokeWidth: 1 }} />
        <Area
          type="monotone"
          dataKey="spend"
          stroke="var(--negative)"
          strokeWidth={2}
          fill="url(#spendGrad)"
          dot={false}
          activeDot={{ r: 4, fill: 'var(--negative)', stroke: 'var(--surface)', strokeWidth: 2 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
