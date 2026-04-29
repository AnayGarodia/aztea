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
import { Wallet, Hammer } from 'lucide-react'
import './DashboardPage.css'
import { fmtDate, fmtUsd } from '../utils/format.js'

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
  const inner = (
    <>
      <div>
        <p className="dashboard__step-title">{title}</p>
        <p className="dashboard__step-copy">{copy}</p>
      </div>
      {actionLabel && (
        <span className={`btn btn--${done ? 'ghost' : 'primary'} btn--sm dashboard__step-action`} aria-hidden="true">
          {actionLabel}
        </span>
      )}
    </>
  )
  if (actionTo) {
    return <Link to={actionTo} className="dashboard__step dashboard__step--link">{inner}</Link>
  }
  return <div className="dashboard__step">{inner}</div>
}

export default function DashboardPage() {
  const { agents, jobs, wallet, loading, apiKey } = useMarket()
  const { user } = useAuth()
  const [reconRuns, setReconRuns] = useState(null)

  const role = user?.role ?? 'both'
  const completedJobs = jobs.filter(j => j.status === 'complete').length
  const activeJobs = jobs.filter(j => j.status === 'running' || j.status === 'pending').length
  const successRate = jobs.length > 0 ? Math.round((completedJobs / jobs.length) * 100) : 0
  const recentJobs = jobs.slice(0, 8)
  const hasBalance = (wallet?.balance_cents ?? 0) > 0
  const isNewUser = !loading && jobs.length === 0 && !hasBalance
  const balance = loading ? '…' : fmtUsd(wallet?.balance_cents ?? 0)
  const isAdmin = (user?.scopes ?? []).includes('admin')
  const creditLabel = role === 'hirer' ? '$2.00' : '$1.00'
  const creditSubtext = role === 'hirer'
    ? "That's enough for about 200 calls at $0.01 each. No card needed to try it."
    : "That's enough for about 100 calls at $0.01 each. No card needed to try it."

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

          {/* Builder welcome banner */}
          {role === 'builder' && isNewUser && (
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
                    <Hammer size={20} color="#fff" />
                  </div>
                  <div>
                    <p style={{ fontWeight: 700, fontSize: '0.9375rem', color: 'var(--ink)', marginBottom: 2 }}>
                      Welcome{user?.username ? `, ${user.username}` : ''} — list your first skill and start earning.
                    </p>
                    <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)' }}>
                      Upload a SKILL.md, set a price, and Aztea handles billing and execution for you.
                    </p>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 'var(--sp-3)', flexShrink: 0 }}>
                  <Link to="/list-skill">
                    <Button variant="primary" size="sm">List a skill</Button>
                  </Link>
                  <Link to="/wallet">
                    <Button variant="secondary" size="sm">View earnings</Button>
                  </Link>
                </div>
              </div>
            </Reveal>
          )}

          {/* Hirer / both starter credit nudge */}
          {role !== 'builder' && isNewUser && (
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
                      Welcome{user?.username ? `, ${user.username}` : ''} — you have {creditLabel} of free credit.
                    </p>
                    <p style={{ fontSize: '0.8125rem', color: 'var(--ink-soft)' }}>
                      {creditSubtext}
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
                <p className="dashboard__welcome-eyebrow t-micro">Overview</p>
                <h1>Welcome{user?.username ? `, ${user.username}` : ''}</h1>
                <p>Hire agents, track jobs, and see every charge and payout in one place.</p>
              </div>
              <div className="dashboard__welcome-actions">
                <Link to="/agents"><Button variant="primary">Browse agents</Button></Link>
                <Link to="/jobs"><Button variant="secondary">View jobs</Button></Link>
              </div>
            </div>
          </Reveal>

          {/* KPIs */}
          <Stagger className="dashboard__kpi-grid" staggerDelay={0.07}>
            {[
              { label: role === 'builder' ? 'Earnings balance' : 'Wallet balance', value: balance, hint: role === 'builder' ? 'Withdraw via Stripe Connect' : '' },
              { label: 'Agents available', value: loading ? '…' : agents.length, hint: role === 'builder' ? 'In the marketplace' : '' },
              { label: 'Active jobs',  value: loading ? '…' : activeJobs, hint: '' },
              { label: 'Success rate', value: loading ? '…' : `${successRate}%`, hint: jobs.length > 0 ? '' : 'No jobs yet' },
            ].filter(Boolean).map(s => (
              <div key={s.label} className="dashboard__kpi">
                <p className="dashboard__kpi-label">{s.label}</p>
                <p className="dashboard__kpi-value">{s.value}</p>
                {s.hint && <p className="dashboard__kpi-hint">{s.hint}</p>}
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
                <span className="dashboard__section-title">Getting started</span>
              </Card.Header>
              <Card.Body className="dashboard__steps">
                {role === 'builder' ? (
                  <>
                    <ActionStep done={false} title="List your first agent" copy="Register an HTTP endpoint or upload a SKILL.md. Set a price per call — Aztea handles billing and delivery." actionTo="/list-skill" actionLabel="List an agent" />
                    <ActionStep done={false} title="Connect Stripe to withdraw earnings" copy="Payouts land in your Aztea wallet after every successful call. Connect a bank account to cash out." actionTo="/wallet" actionLabel="Connect Stripe" />
                    <ActionStep done={false} title="Create a scoped API key" copy="One key per integration so a leak only affects one surface, not your whole account." actionTo="/keys" actionLabel="Manage keys" />
                  </>
                ) : (
                  <>
                    <ActionStep done={agents.length > 0} title="Browse agents to hire" copy="Each listing shows what the agent does, what it costs per call, and example outputs." actionTo="/agents" actionLabel={agents.length > 0 ? 'Browse agents' : 'Start here'} />
                    <ActionStep done={jobs.length > 0} title="Check on your jobs" copy="See pending, running, and completed jobs in one list." actionTo="/jobs" actionLabel="Open jobs" />
                    <ActionStep done={hasBalance} title="Top up your wallet" copy="Every charge, refund, and payout shows up in your wallet transactions." actionTo="/wallet" actionLabel={hasBalance ? 'View wallet' : 'Add funds'} />
                  </>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {/* Recent jobs (full width) */}
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
                    sub="Hire an agent from the marketplace to get started."
                    action={<Link to="/agents"><Button variant="primary">Browse agents</Button></Link>}
                  />
                ) : (
                  <div className="dashboard__jobs">
                    {recentJobs.map(job => <JobRow key={job.job_id} job={job} agents={agents} />)}
                  </div>
                )}
              </Card.Body>
            </Card>
          </Reveal>

        </div>
      </div>
    </main>
  )
}
