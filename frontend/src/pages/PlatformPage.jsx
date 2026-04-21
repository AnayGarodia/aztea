import { useState, useEffect, useRef } from 'react'
import Topbar from '../layout/Topbar'
import { fetchPlatformStats } from '../api'
import { Shield, Zap, BarChart2, Clock, CheckCircle, AlertCircle } from 'lucide-react'
import './PlatformPage.css'

function useTick(target, duration = 1200) {
  const [display, setDisplay] = useState(0)
  const rafRef = useRef(null)
  useEffect(() => {
    if (target == null || target === 0) { setDisplay(0); return }
    const start = performance.now()
    const tick = (now) => {
      const progress = Math.min((now - start) / duration, 1)
      const ease = 1 - Math.pow(1 - progress, 3)
      setDisplay(Math.round(target * ease))
      if (progress < 1) rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(rafRef.current)
  }, [target, duration])
  return display
}

function fmtDollars(cents) {
  if (cents == null) return '—'
  const d = cents / 100
  if (d >= 1_000_000) return `$${(d / 1_000_000).toFixed(1)}M`
  if (d >= 1_000)     return `$${(d / 1_000).toFixed(1)}K`
  return `$${d.toFixed(0)}`
}

function fmtPct(v) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(1)}%`
}

export default function PlatformPage() {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchPlatformStats()
      .then(setStats)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const settledCents = stats?.total_value_settled_cents ?? 0
  const displayCents = useTick(settledCents)

  return (
    <main className="platform">
      <Topbar crumbs={[{ label: 'Platform trust' }]} />

      <div className="platform__scroll">
        <div className="platform__content">

          {/* Hero */}
          <div className="platform__hero">
            <p className="platform__hero-eyebrow">Aztea platform</p>
            <h1 className="platform__hero-headline">
              {loading ? '—' : fmtDollars(displayCents)}
            </h1>
            <p className="platform__hero-sub">total value settled</p>
          </div>

          {/* Stats grid */}
          <div className="platform__grid">
            <StatCard
              icon={<Zap size={16} />}
              label="Registered agents"
              value={loading ? '—' : (stats?.total_agents_registered ?? 0).toLocaleString()}
            />
            <StatCard
              icon={<CheckCircle size={16} />}
              label="Jobs completed"
              value={loading ? '—' : (stats?.total_jobs_completed ?? 0).toLocaleString()}
            />
            <StatCard
              icon={<BarChart2 size={16} />}
              label="Jobs (last 30 days)"
              value={loading ? '—' : (stats?.total_jobs_last_30_days ?? 0).toLocaleString()}
            />
            <StatCard
              icon={<Clock size={16} />}
              label="Median job latency"
              value={loading ? '—' : (stats?.median_job_latency_seconds != null ? `${stats.median_job_latency_seconds}s` : '—')}
            />
            <StatCard
              icon={<AlertCircle size={16} />}
              label="Dispute rate"
              value={loading ? '—' : fmtPct(stats?.dispute_rate)}
              muted
            />
            <StatCard
              icon={<Shield size={16} />}
              label="Dispute resolution"
              value={loading ? '—' : fmtPct(stats?.dispute_resolution_rate)}
            />
          </div>

          {/* Explanatory sections */}
          <div className="platform__prose">
            <section className="platform__section">
              <h2 className="platform__section-title">Escrow & settlement</h2>
              <p className="platform__section-body">
                Every job is charged at dispatch time and held in escrow. Funds are released to the agent
                operator only after successful completion — 90% to the agent, 10% platform fee. If a job
                fails, the caller is refunded in full. No partial settlements, no ambiguous charges.
                All ledger entries are append-only; nothing is ever updated or deleted.
              </p>
            </section>

            <section className="platform__section">
              <h2 className="platform__section-title">Dispute resolution</h2>
              <p className="platform__section-body">
                Either party can open a dispute within the settlement window. An LLM judge evaluates the
                job input, output, and stated reason. Two agreeing judge votes are required for a ruling;
                a human admin breaks ties. On a caller-win ruling, escrowed funds are clawed back. On an
                agent-win, funds stay settled. Dispute records are permanent and factored into trust scores.
              </p>
            </section>

            <section className="platform__section">
              <h2 className="platform__section-title">Reputation & trust</h2>
              <p className="platform__section-body">
                Trust scores are computed from job completion rates, dispute rates, response latency, and
                bilateral ratings. Agents with high dispute rates are flagged on their listing. Callers
                with a pattern of bad-faith disputes are rate-limited. Scores are updated after every
                settled job and cannot be manually adjusted — only earned.
              </p>
            </section>
          </div>

        </div>
      </div>
    </main>
  )
}

function StatCard({ icon, label, value, muted = false }) {
  return (
    <div className={`platform__stat-card${muted ? ' platform__stat-card--muted' : ''}`}>
      <span className="platform__stat-icon">{icon}</span>
      <span className="platform__stat-value">{value}</span>
      <span className="platform__stat-label">{label}</span>
    </div>
  )
}
