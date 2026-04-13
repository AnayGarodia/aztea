import { useMemo } from 'react'
import { motion } from 'framer-motion'
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from 'recharts'
import { useMarket } from '../context/MarketContext'

const TX_LABEL = {
  deposit: 'Deposit',
  charge:  'Charge',
  refund:  'Refund',
  payout:  'Payout',
  fee:     'Fee',
}

const TX_COLOR = {
  deposit: 'var(--positive)',
  charge:  'var(--negative)',
  refund:  'var(--brand)',
  payout:  '#8B5CF6',
  fee:     'var(--neutral-color)',
}

function AmountCell({ amount }) {
  const positive = amount > 0
  return (
    <span style={{
      fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 600,
      color: positive ? 'var(--positive)' : 'var(--negative)',
    }}>
      {positive ? '+' : ''}{amount}¢
    </span>
  )
}

function ActivityTab({ wallet }) {
  const txs = wallet?.transactions ?? []

  if (!txs.length) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100%', color: 'var(--text-muted)', fontSize: 13 }}>
        No transactions yet — make a call to see activity here.
      </div>
    )
  }

  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: '1px solid var(--border)' }}>
          {['Type', 'Memo', 'Amount', 'Time'].map(h => (
            <th key={h} style={{
              padding: '8px 16px', textAlign: 'left',
              fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
              textTransform: 'uppercase', letterSpacing: '0.05em',
            }}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {txs.map((tx, i) => (
          <motion.tr
            key={tx.tx_id}
            initial={i === 0 ? { backgroundColor: 'rgba(92,80,232,0.06)' } : {}}
            animate={{ backgroundColor: 'rgba(92,80,232,0)' }}
            transition={{ duration: 1.2 }}
            style={{ borderBottom: '1px solid var(--border)' }}
          >
            <td style={{ padding: '10px 16px' }}>
              <span style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                padding: '2px 8px', borderRadius: 4,
                background: 'var(--bg)', border: '1px solid var(--border)',
                fontSize: 12, fontWeight: 500,
                color: TX_COLOR[tx.type] ?? 'var(--text-secondary)',
              }}>
                {TX_LABEL[tx.type] ?? tx.type}
              </span>
            </td>
            <td style={{ padding: '10px 16px', color: 'var(--text-secondary)', maxWidth: 240 }}>
              <span style={{ display: 'block', overflow: 'hidden',
                textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {tx.memo || '—'}
              </span>
            </td>
            <td style={{ padding: '10px 16px' }}>
              <AmountCell amount={tx.amount_cents} />
            </td>
            <td style={{ padding: '10px 16px', color: 'var(--text-muted)',
              fontFamily: 'var(--font-mono)', fontSize: 12 }}>
              {new Date(tx.created_at).toLocaleTimeString('en', {
                hour12: false, hour: '2-digit', minute: '2-digit',
              })}
            </td>
          </motion.tr>
        ))}
      </tbody>
    </table>
  )
}

const tooltipStyle = {
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 6,
  fontSize: 12,
  fontFamily: 'var(--font-sans)',
  color: 'var(--text-primary)',
  boxShadow: '0 4px 8px rgba(0,0,0,0.08)',
}
const axisStyle = { fontSize: 11, fill: 'var(--text-muted)', fontFamily: 'var(--font-sans)' }

function AnalyticsTab({ wallet, runs, agents }) {
  const balanceData = useMemo(() => {
    if (!wallet?.transactions?.length) return []
    let running = 0
    return [...wallet.transactions].reverse().map(tx => {
      running += tx.amount_cents
      return {
        time: new Date(tx.created_at).toLocaleTimeString('en', {
          hour12: false, hour: '2-digit', minute: '2-digit',
        }),
        balance: running,
      }
    })
  }, [wallet?.transactions])

  const latencyData = useMemo(() =>
    runs.slice(0, 15).reverse().map(r => ({
      ticker: r.ticker,
      latency: parseFloat((r.latency_seconds).toFixed(1)),
      signal: r.output?.signal,
    })),
    [runs]
  )

  const signalCounts = useMemo(() => {
    const c = { positive: 0, neutral: 0, negative: 0 }
    runs.forEach(r => { if (r.output?.signal) c[r.output.signal]++ })
    return c
  }, [runs])

  const totalRuns = runs.length

  if (!totalRuns && !wallet?.transactions?.length) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
        height: '100%', color: 'var(--text-muted)', fontSize: 13 }}>
        No data yet — make some calls to see analytics.
      </div>
    )
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', height: '100%' }}>
      {/* Balance chart */}
      <div style={{ padding: '12px 16px', borderRight: '1px solid var(--border)' }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
          Wallet balance
        </div>
        {balanceData.length > 1 ? (
          <ResponsiveContainer width="100%" height={140}>
            <AreaChart data={balanceData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <defs>
                <linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="10%" stopColor="#5C50E8" stopOpacity={0.15} />
                  <stop offset="95%" stopColor="#5C50E8" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="time" tick={axisStyle} interval="preserveStartEnd" tickLine={false} />
              <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={32} />
              <Tooltip contentStyle={tooltipStyle} formatter={v => [`${v}¢`, 'Balance']} />
              <Area type="monotone" dataKey="balance" stroke="var(--brand)"
                strokeWidth={2} fill="url(#balGrad)" dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div style={{ height: 140, display: 'flex', alignItems: 'center',
            justifyContent: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
            Need 2+ transactions
          </div>
        )}
      </div>

      {/* Latency chart */}
      <div style={{ padding: '12px 16px', borderRight: '1px solid var(--border)' }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 10 }}>
          Call latency (s)
        </div>
        {latencyData.length > 0 ? (
          <ResponsiveContainer width="100%" height={140}>
            <BarChart data={latencyData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="ticker" tick={axisStyle} tickLine={false} />
              <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={28} unit="s" />
              <Tooltip contentStyle={tooltipStyle}
                formatter={(v, _, p) => [`${v}s`, p.payload.ticker]} />
              <Bar dataKey="latency" radius={[3, 3, 0, 0]}>
                {latencyData.map((d, i) => (
                  <Cell key={i} fill={
                    d.signal === 'positive' ? '#059669' :
                    d.signal === 'negative' ? '#DC2626' : '#D97706'
                  } />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div style={{ height: 140, display: 'flex', alignItems: 'center',
            justifyContent: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
            No runs yet
          </div>
        )}
      </div>

      {/* Signal summary */}
      <div style={{ padding: '12px 16px' }}>
        <div style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-muted)',
          textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 12 }}>
          Signal breakdown
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[
            { key: 'positive', label: 'Positive', color: 'var(--positive)',
              bg: 'var(--positive-bg)', border: 'var(--positive-border)' },
            { key: 'neutral',  label: 'Neutral',  color: 'var(--neutral-color)',
              bg: 'var(--neutral-bg)', border: 'var(--neutral-border)' },
            { key: 'negative', label: 'Negative', color: 'var(--negative)',
              bg: 'var(--negative-bg)', border: 'var(--negative-border)' },
          ].map(s => {
            const count = signalCounts[s.key]
            const pct = totalRuns > 0 ? Math.round((count / totalRuns) * 100) : 0
            return (
              <div key={s.key}>
                <div style={{ display: 'flex', justifyContent: 'space-between',
                  marginBottom: 4, fontSize: 13 }}>
                  <span style={{ color: 'var(--text-secondary)' }}>{s.label}</span>
                  <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 600,
                    color: 'var(--text-primary)' }}>
                    {count} <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>({pct}%)</span>
                  </span>
                </div>
                <div style={{ height: 6, background: 'var(--border)', borderRadius: 3, overflow: 'hidden' }}>
                  <motion.div
                    initial={{ width: 0 }}
                    animate={{ width: `${pct}%` }}
                    transition={{ duration: 0.6, ease: [0.25, 0.1, 0.25, 1] }}
                    style={{ height: '100%', background: s.color, borderRadius: 3 }}
                  />
                </div>
              </div>
            )
          })}
          <div style={{ marginTop: 4, fontSize: 12, color: 'var(--text-muted)' }}>
            {totalRuns} total run{totalRuns !== 1 ? 's' : ''}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function ActivityPanel({ tab }) {
  const { wallet, runs, agents } = useMarket()
  return tab === 'activity'
    ? <ActivityTab wallet={wallet} />
    : <AnalyticsTab wallet={wallet} runs={runs} agents={agents} />
}
