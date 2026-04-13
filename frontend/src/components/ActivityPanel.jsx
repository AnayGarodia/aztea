import { useMemo } from 'react'
import { motion } from 'framer-motion'
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { useMarket } from '../context/MarketContext'

// ── Constants ──────────────────────────────────────────────────────────────────

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
  fee:     'var(--text-muted)',
}

const JOB_STATUS_META = {
  pending:               { label: 'Pending',     color: 'var(--neutral-color)',  bg: 'var(--neutral-bg)',   pulse: false },
  running:               { label: 'Running',     color: 'var(--brand)',          bg: 'rgba(0,212,168,0.08)', pulse: true  },
  awaiting_clarification:{ label: 'Awaiting',    color: '#F59E0B',               bg: 'rgba(245,158,11,0.08)', pulse: false },
  complete:              { label: 'Complete',    color: 'var(--positive)',       bg: 'var(--positive-bg)',  pulse: false },
  failed:                { label: 'Failed',      color: 'var(--negative)',       bg: 'var(--negative-bg)', pulse: false },
}

// ── Shared styles ──────────────────────────────────────────────────────────────

const tooltipStyle = {
  background: 'var(--surface)',
  border: '1px solid var(--border-bright)',
  borderRadius: 6,
  fontSize: 12,
  fontFamily: 'var(--font-sans)',
  color: 'var(--text-primary)',
  boxShadow: '0 8px 24px rgba(0,0,0,0.4)',
}
const axisStyle = { fontSize: 11, fill: 'var(--text-muted)', fontFamily: 'var(--font-sans)' }

const emptyState = msg => (
  <div style={{
    display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
    height: '100%', gap: 8,
  }}>
    <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
      stroke="var(--border-bright)" strokeWidth="1.5">
      <circle cx="12" cy="12" r="10"/>
      <path d="M12 8v4m0 4h.01"/>
    </svg>
    <span style={{ color: 'var(--text-muted)', fontSize: 13 }}>{msg}</span>
  </div>
)

// ── Shared cell helpers ────────────────────────────────────────────────────────

function UsdCell({ cents }) {
  const positive = cents >= 0
  return (
    <span style={{
      fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 600,
      color: positive ? 'var(--positive)' : 'var(--negative)',
    }}>
      {positive ? '+' : '-'}${Math.abs(cents / 100).toFixed(2)}
    </span>
  )
}

function TimeCell({ iso }) {
  if (!iso) return <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>—</span>
  const d = new Date(iso)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const timeStr = d.toLocaleTimeString('en', { hour12: false, hour: '2-digit', minute: '2-digit' })
  const dateStr = sameDay ? timeStr : d.toLocaleDateString('en', { month: 'short', day: 'numeric' }) + ' ' + timeStr
  return (
    <span style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>
      {dateStr}
    </span>
  )
}

// ── Transactions tab ───────────────────────────────────────────────────────────

function TransactionsTab({ wallet }) {
  const txs = wallet?.transactions ?? []

  if (!txs.length) return emptyState('No transactions yet — make a call to see activity here.')

  return (
    <div style={{ overflowY: 'auto', height: '100%' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead style={{ position: 'sticky', top: 0, background: 'var(--surface)', zIndex: 1 }}>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            {['Type', 'Memo', 'Amount', 'Time'].map(h => (
              <th key={h} style={{
                padding: '8px 16px', textAlign: 'left',
                fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
                textTransform: 'uppercase', letterSpacing: '0.07em',
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
              initial={i === 0 ? { backgroundColor: 'rgba(0,212,168,0.05)' } : {}}
              animate={{ backgroundColor: 'rgba(0,212,168,0)' }}
              transition={{ duration: 1.4 }}
              style={{ borderBottom: '1px solid var(--border)' }}
            >
              <td style={{ padding: '9px 16px' }}>
                <span style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '2px 8px', borderRadius: 4,
                  background: 'var(--bg)', border: '1px solid var(--border)',
                  fontSize: 11, fontWeight: 600,
                  color: TX_COLOR[tx.type] ?? 'var(--text-secondary)',
                  letterSpacing: '0.03em',
                }}>
                  {TX_LABEL[tx.type] ?? tx.type}
                </span>
              </td>
              <td style={{ padding: '9px 16px', color: 'var(--text-secondary)', maxWidth: 220 }}>
                <span style={{
                  display: 'block', overflow: 'hidden',
                  textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}>
                  {tx.memo || '—'}
                </span>
              </td>
              <td style={{ padding: '9px 16px' }}>
                <UsdCell cents={tx.amount_cents} />
              </td>
              <td style={{ padding: '9px 16px' }}>
                <TimeCell iso={tx.created_at} />
              </td>
            </motion.tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Jobs tab ───────────────────────────────────────────────────────────────────

function JobStatusBadge({ status }) {
  const meta = JOB_STATUS_META[status] ?? { label: status, color: 'var(--text-muted)', bg: 'var(--bg)', pulse: false }
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '2px 8px', borderRadius: 4,
      background: meta.bg, fontSize: 11, fontWeight: 600,
      color: meta.color, letterSpacing: '0.03em',
    }}>
      {meta.pulse && (
        <span style={{
          width: 5, height: 5, borderRadius: '50%',
          background: meta.color, animation: 'ping 1.5s cubic-bezier(0,0,0.2,1) infinite',
          flexShrink: 0,
        }} />
      )}
      {meta.label}
    </span>
  )
}

function JobsTab({ jobs, agents }) {
  if (!jobs.length) return emptyState('No jobs yet — submit an async call to create one.')

  const agentMap = useMemo(() =>
    Object.fromEntries(agents.map(a => [a.agent_id, a.name])),
    [agents]
  )

  return (
    <div style={{ overflowY: 'auto', height: '100%' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead style={{ position: 'sticky', top: 0, background: 'var(--surface)', zIndex: 1 }}>
          <tr style={{ borderBottom: '1px solid var(--border)' }}>
            {['Agent', 'Status', 'Cost', 'Created', 'Completed'].map(h => (
              <th key={h} style={{
                padding: '8px 16px', textAlign: 'left',
                fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
                textTransform: 'uppercase', letterSpacing: '0.07em',
              }}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {jobs.map((job, i) => (
            <motion.tr
              key={job.job_id}
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.2, delay: i * 0.03 }}
              style={{ borderBottom: '1px solid var(--border)' }}
            >
              <td style={{ padding: '9px 16px', maxWidth: 180 }}>
                <span style={{
                  display: 'block', overflow: 'hidden',
                  textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  color: 'var(--text-primary)', fontWeight: 500,
                }}>
                  {agentMap[job.agent_id] ?? job.agent_id?.slice(0, 8) + '…'}
                </span>
                <span style={{
                  fontSize: 10, color: 'var(--text-muted)',
                  fontFamily: 'var(--font-mono)',
                }}>
                  {job.job_id?.slice(0, 8)}…
                </span>
              </td>
              <td style={{ padding: '9px 16px' }}>
                <JobStatusBadge status={job.status} />
              </td>
              <td style={{ padding: '9px 16px' }}>
                {job.price_cents != null
                  ? <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-secondary)' }}>
                      ${(job.price_cents / 100).toFixed(3)}
                    </span>
                  : <span style={{ color: 'var(--text-muted)' }}>—</span>
                }
              </td>
              <td style={{ padding: '9px 16px' }}>
                <TimeCell iso={job.created_at} />
              </td>
              <td style={{ padding: '9px 16px' }}>
                <TimeCell iso={job.completed_at} />
              </td>
            </motion.tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ── Analytics tab ──────────────────────────────────────────────────────────────

function StatTile({ label, value, sub, accent }) {
  return (
    <div style={{
      padding: '14px 16px',
      background: 'var(--bg)',
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-md)',
      display: 'flex', flexDirection: 'column', gap: 4,
    }}>
      <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
        textTransform: 'uppercase', letterSpacing: '0.07em' }}>
        {label}
      </span>
      <span style={{
        fontFamily: 'var(--font-mono)', fontSize: 22, fontWeight: 700,
        color: accent ?? 'var(--text-primary)', lineHeight: 1,
      }}>
        {value}
      </span>
      {sub && <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{sub}</span>}
    </div>
  )
}

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
        balance: parseFloat((running / 100).toFixed(2)),
      }
    })
  }, [wallet?.transactions])

  // Generic latency data — label by run index or agent name
  const agentMap = useMemo(() =>
    Object.fromEntries(agents.map(a => [a.agent_id, a.name])),
    [agents]
  )

  const latencyData = useMemo(() =>
    runs.slice(0, 15).reverse().map((r, i) => ({
      label: agentMap[r.agent_id] ?? (r.ticker ?? `#${i + 1}`),
      latency: parseFloat((r.latency_seconds ?? 0).toFixed(1)),
    })),
    [runs, agentMap]
  )

  // Generic success stats
  const successRate = useMemo(() => {
    if (!runs.length) return null
    const successful = runs.filter(r => r.status !== 'failed' && r.output).length
    return Math.round((successful / runs.length) * 100)
  }, [runs])

  const avgLatency = useMemo(() => {
    if (!runs.length) return null
    const valid = runs.filter(r => r.latency_seconds != null)
    if (!valid.length) return null
    return (valid.reduce((s, r) => s + r.latency_seconds, 0) / valid.length).toFixed(1)
  }, [runs])

  const spent = useMemo(() => {
    if (!wallet?.transactions) return 0
    return Math.abs(wallet.transactions
      .filter(tx => tx.type === 'charge')
      .reduce((s, tx) => s + tx.amount_cents, 0))
  }, [wallet?.transactions])

  const hasData = runs.length > 0 || (wallet?.transactions?.length ?? 0) > 0

  if (!hasData) return emptyState('No data yet — make some calls to see analytics.')

  return (
    <div style={{ overflowY: 'auto', height: '100%', padding: '14px 16px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Stat tiles */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10 }}>
        <StatTile label="Total calls" value={runs.length} sub="all time" />
        <StatTile
          label="Success rate"
          value={successRate != null ? `${successRate}%` : '—'}
          sub="non-failed runs"
          accent={successRate != null && successRate >= 80 ? 'var(--positive)' : successRate != null && successRate < 50 ? 'var(--negative)' : undefined}
        />
        <StatTile
          label="Avg latency"
          value={avgLatency != null ? `${avgLatency}s` : '—'}
          sub="across all runs"
        />
        <StatTile
          label="Total spent"
          value={`$${(spent / 100).toFixed(2)}`}
          sub="platform charges"
          accent="var(--negative)"
        />
      </div>

      {/* Charts row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, flex: 1 }}>
        {/* Balance over time */}
        <div style={{
          padding: '12px 14px',
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-md)',
        }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 12 }}>
            Wallet balance (USD)
          </div>
          {balanceData.length > 1 ? (
            <ResponsiveContainer width="100%" height={140}>
              <AreaChart data={balanceData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="balGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="10%" stopColor="var(--brand)" stopOpacity={0.2} />
                    <stop offset="95%" stopColor="var(--brand)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="time" tick={axisStyle} interval="preserveStartEnd" tickLine={false} />
                <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={40}
                  tickFormatter={v => `$${v}`} />
                <Tooltip contentStyle={tooltipStyle} formatter={v => [`$${v}`, 'Balance']} />
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

        {/* Latency per run */}
        <div style={{
          padding: '12px 14px',
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-md)',
        }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)',
            textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 12 }}>
            Call latency (s)
          </div>
          {latencyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={140}>
              <BarChart data={latencyData} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                <XAxis dataKey="label" tick={axisStyle} tickLine={false}
                  interval={0}
                  tickFormatter={v => v.length > 8 ? v.slice(0, 7) + '…' : v} />
                <YAxis tick={axisStyle} tickLine={false} axisLine={false} width={32} unit="s" />
                <Tooltip contentStyle={tooltipStyle}
                  formatter={(v, _, p) => [`${v}s`, p.payload.label]} />
                <Bar dataKey="latency" radius={[3, 3, 0, 0]} fill="var(--brand)" opacity={0.8} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div style={{ height: 140, display: 'flex', alignItems: 'center',
              justifyContent: 'center', color: 'var(--text-muted)', fontSize: 12 }}>
              No runs yet
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Root export ────────────────────────────────────────────────────────────────

export default function ActivityPanel({ tab }) {
  const { wallet, runs, agents, jobs } = useMarket()

  if (tab === 'activity')  return <TransactionsTab wallet={wallet} />
  if (tab === 'jobs')      return <JobsTab jobs={jobs} agents={agents} />
  return <AnalyticsTab wallet={wallet} runs={runs} agents={agents} />
}
