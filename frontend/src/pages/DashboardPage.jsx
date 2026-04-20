import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import { useMarket } from '../context/MarketContext'
import { useAuth } from '../context/AuthContext'
import { fetchReconciliationRuns } from '../api'
import { Wallet } from 'lucide-react'
import './DashboardPage.css'

function fmtDate(str) {
  if (!str) return '--'
  return new Date(str).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function JobRow({ job, agents }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  const isLive = job.status === 'running' || job.status === 'pending'
  return (
    <Link to={`/jobs/${job.job_id}`} className="dashboard__job-row">
      <div>
        <p className="dashboard__job-name">{agent?.name ?? 'Unknown agent'}</p>
        <p className="dashboard__job-id t-mono">{job.job_id.slice(0, 12)}…</p>
      </div>
      <Badge label={job.status} dot />
      <p className="dashboard__job-date">{fmtDate(job.created_at)}</p>
    </Link>
  )
}

function ActionStep({ done, title, copy, actionTo, actionLabel }) {
  return (
    <div className="dashboard__step">
      <div>
        <p className="dashboard__step-title">{title}</p>
        <p className="dashboard__step-copy">{copy}</p>
      </div>
      {actionTo && (
        <Link to={actionTo}>
          <Button size="sm" variant={done ? 'ghost' : 'primary'}>{actionLabel}</Button>
        </Link>
      )}
    </div>
  )
}

export default function DashboardPage() {
  const { agents, jobs, wallet, loading, apiKey } = useMarket()
  const { user } = useAuth()
  const [reconRuns, setReconRuns] = useState(null)

  const completedJobs = jobs.filter(j => j.status === 'complete').length
  const activeJobs = jobs.filter(j => j.status === 'running' || j.status === 'pending').length
  const successRate = jobs.length > 0 ? Math.round((completedJobs / jobs.length) * 100) : 0
  const recentJobs = jobs.slice(0, 8)
  const hasBalance = (wallet?.balance_cents ?? 0) > 0
  const isNewUser = !loading && jobs.length === 0 && !hasBalance
  const balance = loading ? '…' : `$${((wallet?.balance_cents ?? 0) / 100).toFixed(2)}`
  const isAdmin = (user?.scopes ?? []).includes('admin')

  useEffect(() => {
    if (!isAdmin || !apiKey) {
      setReconRuns(null)
      return
    }
    let active = true
    fetchReconciliationRuns(apiKey, 5)
      .then(data => {
        if (!active) return
        setReconRuns(data?.runs ?? [])
      })
      .catch(() => {
        if (!active) return
        setReconRuns([])
      })
    return () => { active = false }
  }, [isAdmin, apiKey])

  return (
    <main className="dashboard">
      <Topbar crumbs={[{ label: 'Overview' }]} />

      <div className="dashboard__scroll">
        <div className="dashboard__content">

          {/* Starter credit nudge — shown only to brand-new users with $0 balance */}
          {isNewUser && (
            <Reveal>
              <div style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                gap: 'var(--sp-5)', flexWrap: 'wrap',
                padding: 'var(--sp-5)',
                background: 'var(--accent-wash)',
                border: '1px solid var(--accent-line)',
                borderRadius: 'var(--r-lg)',
                marginBottom: 'var(--sp-5)',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-4)' }}>
                  <div style={{
                    width: 40, height: 40, borderRadius: 'var(--r-md)',
                    background: 'var(--accent)', display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                  }}>
                    <Wallet size={20} color="#fff" />
                  </div>
                  <div>
                    <p style={{ fontWeight: 700, fontSize: '0.9375rem', color: 'var(--ink)', marginBottom: 2 }}>
                      Welcome{user?.username ? `, ${user.username}` : ''}! You have a $1.00 starter credit.
                    </p>
                    <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)' }}>
                      Your wallet is ready — browse agents and make your first call at $0.01.
                    </p>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 'var(--sp-3)', flexShrink: 0 }}>
                  <Link to="/agents">
                    <Button variant="primary" size="sm">Browse agents</Button>
                  </Link>
                  <Link to="/wallet">
                    <Button variant="secondary" size="sm">View wallet</Button>
                  </Link>
                </div>
              </div>
            </Reveal>
          )}

          {/* Welcome */}
          <Reveal>
            <div className="dashboard__welcome">
              <div>
                <p className="dashboard__welcome-eyebrow t-micro">Control center</p>
                <h1>Welcome{user?.username ? `, ${user.username}` : ''}</h1>
                <p>Discover agents, run jobs, and track outcomes from one place.</p>
              </div>
              <div className="dashboard__welcome-actions">
                <Link to="/agents"><Button variant="primary">Discover agents</Button></Link>
                <Link to="/jobs"><Button variant="secondary">Monitor jobs</Button></Link>
              </div>
            </div>
          </Reveal>

          {/* KPIs */}
          <Stagger className="dashboard__kpi-grid" staggerDelay={0.07}>
            {[
              { label: 'Wallet balance', value: balance, hint: 'Available for calls' },
              { label: 'Agents live',    value: loading ? '…' : agents.length, hint: 'Available to hire' },
              { label: 'Active jobs',   value: loading ? '…' : activeJobs, hint: 'Running or pending' },
              { label: 'Success rate',  value: loading ? '…' : `${successRate}%`, hint: jobs.length > 0 ? `${completedJobs}/${jobs.length} completed` : 'No jobs yet' },
            ].map(s => (
              <div key={s.label} className="dashboard__kpi">
                <p className="dashboard__kpi-label">{s.label}</p>
                <p className="dashboard__kpi-value">{s.value}</p>
                <p className="dashboard__kpi-hint">{s.hint}</p>
              </div>
            ))}
          </Stagger>

          {isAdmin && (
            <Reveal delay={0.08}>
              <Card>
                <Card.Header>
                  <span className="dashboard__section-title">Ledger reconciliation</span>
                </Card.Header>
                <Card.Body>
                  {reconRuns === null ? (
                    <p className="dashboard__kpi-hint">Loading reconciliation runs…</p>
                  ) : reconRuns.length === 0 ? (
                    <p className="dashboard__kpi-hint">No reconciliation runs found yet.</p>
                  ) : (
                    <div>
                      <p className="dashboard__kpi-hint">
                        Latest: {fmtDate(reconRuns[0]?.created_at)} · drift {(reconRuns[0]?.drift_cents ?? 0)}¢ · mismatches {(reconRuns[0]?.mismatch_count ?? 0)}
                      </p>
                      <div className="dashboard__trust-pills" style={{ marginTop: 'var(--sp-2)' }}>
                        <Badge label={reconRuns[0]?.invariant_ok ? 'invariant_ok' : 'invariant_failed'} dot />
                      </div>
                    </div>
                  )}
                </Card.Body>
              </Card>
            </Reveal>
          )}

          {/* Checklist */}
          <Reveal delay={0.1}>
            <Card>
              <Card.Header>
                <span className="dashboard__section-title">Getting started checklist</span>
              </Card.Header>
              <Card.Body className="dashboard__steps">
                <ActionStep done={agents.length > 0} title="Discover marketplace listings" copy="Compare capabilities, trust signals, and pricing." actionTo="/agents" actionLabel={agents.length > 0 ? 'Browse agents' : 'Start here'} />
                <ActionStep done={jobs.length > 0} title="Create your first call or job" copy="Open an agent, submit schema-based input, choose sync or async." actionTo="/agents" actionLabel={jobs.length > 0 ? 'Run another' : 'First job'} />
                <ActionStep done={jobs.length > 0} title="Monitor status and outputs" copy="Track pending / running / completed jobs in one timeline." actionTo="/jobs" actionLabel="Open jobs" />
                <ActionStep done={hasBalance} title="Keep wallet funded and auditable" copy="Charges, refunds, and payouts are visible in wallet transactions." actionTo="/wallet" actionLabel={hasBalance ? 'View wallet' : 'Add funds'} />
              </Card.Body>
            </Card>
          </Reveal>

          {/* Main grid */}
          <div className="dashboard__main-grid">
            <Reveal delay={0.15}>
              <Card>
                <Card.Header className="dashboard__panel-head">
                  <span className="dashboard__section-title">Recent jobs</span>
                  <Link to="/jobs"><Button variant="ghost" size="sm">View all</Button></Link>
                </Card.Header>
                <Card.Body>
                  {loading ? (
                    <div className="dashboard__loading-list">
                      {[1,2,3,4].map(i => <Skeleton key={i} variant="rect" height={52} />)}
                    </div>
                  ) : recentJobs.length === 0 ? (
                    <EmptyState
                      agentId="empty-jobs"
                      title="No jobs yet"
                      sub="Start by hiring an agent from the marketplace."
                      action={<Link to="/agents"><Button variant="primary">Discover agents</Button></Link>}
                    />
                  ) : (
                    <div className="dashboard__jobs">
                      {recentJobs.map(job => <JobRow key={job.job_id} job={job} agents={agents} />)}
                    </div>
                  )}
                </Card.Body>
              </Card>
            </Reveal>

            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <span className="dashboard__section-title">Payment model</span>
                </Card.Header>
                <Card.Body className="dashboard__trust">
                  <p>Calls are charged from your wallet before execution. Successful jobs pay agents automatically; failures are fully refunded.</p>
                  <div className="dashboard__trust-pills">
                    <Badge label="deposit" dot />
                    <Badge label="payout" dot />
                    <Badge label="refund" dot />
                  </div>
                  <Link to="/wallet">
                    <Button variant="secondary" size="sm">Open wallet ledger</Button>
                  </Link>
                </Card.Body>
              </Card>
            </Reveal>
          </div>

        </div>
      </div>
    </main>
  )
}
