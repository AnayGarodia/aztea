import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import { useMarket } from '../context/MarketContext'
import { useAuth } from '../context/AuthContext'
import { Wallet } from 'lucide-react'
import './DashboardPage.css'

function fmtDate(str) {
  if (!str) return '--'
  return new Date(str).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function StatCard({ label, value, hint }) {
  return (
    <Card>
      <Card.Body className="dashboard__stat-body">
        <p className="dashboard__stat-label">{label}</p>
        <p className="dashboard__stat-value">{value}</p>
        {hint && <p className="dashboard__stat-hint">{hint}</p>}
      </Card.Body>
    </Card>
  )
}

function JobRow({ job, agents }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  return (
    <Link to={`/jobs/${job.job_id}`} className="dashboard__job-row">
      <div>
        <p className="dashboard__job-name">{agent?.name ?? 'Unknown agent'}</p>
        <p className="dashboard__job-id">{job.job_id.slice(0, 12)}…</p>
      </div>
      <Badge label={job.status} dot />
      <p className="dashboard__job-date">{fmtDate(job.created_at)}</p>
    </Link>
  )
}

function ActionStep({ done, title, copy, actionTo, actionLabel }) {
  return (
    <div className="dashboard__step">
      <span className={`dashboard__step-dot ${done ? 'dashboard__step-dot--done' : ''}`} aria-hidden="true" />
      <div>
        <p className="dashboard__step-title">{title}</p>
        <p className="dashboard__step-copy">{copy}</p>
      </div>
      {actionTo && (
        <Link to={actionTo}>
          <Button size="sm" variant={done ? 'secondary' : 'primary'}>{actionLabel}</Button>
        </Link>
      )}
    </div>
  )
}

export default function DashboardPage() {
  const { agents, jobs, wallet, loading } = useMarket()
  const { user } = useAuth()

  const completedJobs = jobs.filter(j => j.status === 'complete').length
  const activeJobs = jobs.filter(j => j.status === 'running' || j.status === 'pending').length
  const successRate = jobs.length > 0 ? Math.round((completedJobs / jobs.length) * 100) : 0
  const recentJobs = jobs.slice(0, 8)
  const hasBalance = (wallet?.balance_cents ?? 0) > 0
  const isNewUser = !loading && jobs.length === 0 && !hasBalance

  const stats = useMemo(() => ([
    { label: 'Wallet balance', value: loading ? '…' : `$${((wallet?.balance_cents ?? 0) / 100).toFixed(2)}`, hint: 'Funds available for calls' },
    { label: 'Agents live', value: loading ? '…' : agents.length, hint: 'Listings available to hire' },
    { label: 'Active jobs', value: loading ? '…' : activeJobs, hint: 'Running or pending' },
    { label: 'Success rate', value: loading ? '…' : `${successRate}%`, hint: jobs.length > 0 ? `${completedJobs}/${jobs.length} completed` : 'No jobs yet' },
  ]), [loading, wallet?.balance_cents, agents.length, activeJobs, successRate, jobs.length, completedJobs])

  return (
    <main className="dashboard">
      <Topbar crumbs={[{ label: 'Overview' }]} />

      <div className="dashboard__scroll">
        <div className="dashboard__content">

          {/* Welcome nudge — shown only to brand-new users with $0 balance */}
          {isNewUser && (
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
          )}

          <header className="dashboard__header">
            <div>
              <p className="dashboard__eyebrow">Control center</p>
              <h1>Launch-ready workflow</h1>
              <p>Everything you need to discover agents, create jobs, monitor outcomes, and manage wallet trust.</p>
            </div>
            <div className="dashboard__header-actions">
              <Link to="/agents"><Button variant="primary">Discover agents</Button></Link>
              <Link to="/jobs"><Button variant="secondary">Monitor jobs</Button></Link>
            </div>
          </header>

          <Card>
            <Card.Header>
              <span className="dashboard__section-title">How it works (first-time checklist)</span>
            </Card.Header>
            <Card.Body className="dashboard__steps">
              <ActionStep
                done={agents.length > 0}
                title="1) Discover marketplace listings"
                copy="Compare capabilities, trust signals, and pricing in the Agents tab."
                actionTo="/agents"
                actionLabel={agents.length > 0 ? 'Review agents' : 'Browse agents'}
              />
              <ActionStep
                done={jobs.length > 0}
                title="2) Create your first call or async job"
                copy="Open an agent profile, submit schema-based input, and choose sync or async."
                actionTo="/agents"
                actionLabel={jobs.length > 0 ? 'Run another' : 'Create first job'}
              />
              <ActionStep
                done={jobs.length > 0}
                title="3) Monitor status and outputs"
                copy="Track pending/running/completed jobs in one timeline."
                actionTo="/jobs"
                actionLabel="Open jobs"
              />
              <ActionStep
                done={hasBalance}
                title="4) Keep wallet funded and auditable"
                copy="Charges, refunds, and payouts are visible in wallet transactions."
                actionTo="/wallet"
                actionLabel={hasBalance ? 'View wallet' : 'Add funds'}
              />
            </Card.Body>
          </Card>

          <section className="dashboard__stat-grid">
            {stats.map((s) => (
              <StatCard key={s.label} label={s.label} value={s.value} hint={s.hint} />
            ))}
          </section>

          <section className="dashboard__main-grid">
            <Card>
              <Card.Header className="dashboard__panel-head">
                <span className="dashboard__section-title">Recent jobs</span>
                <Link to="/jobs"><Button variant="ghost" size="sm">View all</Button></Link>
              </Card.Header>
              <Card.Body>
                {loading ? (
                  <div className="dashboard__loading-list">
                    {[1, 2, 3, 4].map(i => <Skeleton key={i} variant="rect" height={52} />)}
                  </div>
                ) : recentJobs.length === 0 ? (
                  <EmptyState
                    title="No jobs yet"
                    sub="Start by hiring an agent from the marketplace."
                    action={<Link to="/agents"><Button variant="primary">Discover agents</Button></Link>}
                  />
                ) : (
                  <div className="dashboard__jobs">
                    {recentJobs.map(job => (
                      <JobRow key={job.job_id} job={job} agents={agents} />
                    ))}
                  </div>
                )}
              </Card.Body>
            </Card>

            <Card>
              <Card.Header>
                <span className="dashboard__section-title">Trust + wallet context</span>
              </Card.Header>
              <Card.Body className="dashboard__trust">
                <p>
                  Calls are charged from your wallet before execution.
                  Successful jobs pay agents automatically; failures are refunded.
                </p>
                <div className="dashboard__trust-pills">
                  <Badge label="Auto charge" dot />
                  <Badge label="Auto payout" dot />
                  <Badge label="Refund on failure" dot />
                </div>
                <Link to="/wallet">
                  <Button variant="secondary" size="sm">Open wallet ledger</Button>
                </Link>
              </Card.Body>
            </Card>
          </section>
        </div>
      </div>
    </main>
  )
}
