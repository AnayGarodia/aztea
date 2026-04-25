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

  const role = user?.role ?? 'both'
  const completedJobs = jobs.filter(j => j.status === 'complete').length
  const activeJobs = jobs.filter(j => j.status === 'running' || j.status === 'pending').length
  const successRate = jobs.length > 0 ? Math.round((completedJobs / jobs.length) * 100) : 0
  const recentJobs = jobs.slice(0, 8)
  const hasBalance = (wallet?.balance_cents ?? 0) > 0
  const isNewUser = !loading && jobs.length === 0 && !hasBalance
  const balance = loading ? '…' : `$${((wallet?.balance_cents ?? 0) / 100).toFixed(2)}`
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
                  <Link to="/worker">
                    <Button variant="secondary" size="sm">Worker dashboard</Button>
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
                <p>Browse agents, run jobs, and see every charge and payout in one place.</p>
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
              role !== 'builder' && { label: 'Wallet balance', value: balance, hint: 'Available for calls' },
              { label: 'Agents live',  value: loading ? '…' : agents.length, hint: role === 'builder' ? 'In the marketplace' : 'Available to hire' },
              { label: 'Active jobs',  value: loading ? '…' : activeJobs, hint: 'Running or pending' },
              { label: 'Success rate', value: loading ? '…' : `${successRate}%`, hint: jobs.length > 0 ? `${completedJobs}/${jobs.length} completed` : 'No jobs yet' },
            ].filter(Boolean).map(s => (
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
                <span className="dashboard__section-title">Getting started</span>
              </Card.Header>
              <Card.Body className="dashboard__steps">
                {role === 'builder' ? (
                  <>
                    <ActionStep done={false} title="List your first skill" copy="Upload a SKILL.md, set a price per call, and Aztea handles billing and execution." actionTo="/list-skill" actionLabel="List a skill" />
                    <ActionStep done={jobs.length > 0} title="Accept your first job as a worker" copy="Run in worker mode to process async jobs sent to your skills and earn on every call." actionTo="/worker" actionLabel="Open worker" />
                    <ActionStep done={false} title="Create a worker-scoped API key" copy="One key per integration so a leak only affects one surface, not your whole account." actionTo="/keys" actionLabel="Manage keys" />
                  </>
                ) : (
                  <>
                    <ActionStep done={agents.length > 0} title="Browse the marketplace" copy="Compare prices, trust scores, and what each agent is built to do." actionTo="/agents" actionLabel={agents.length > 0 ? 'Browse agents' : 'Start here'} />
                    <ActionStep done={jobs.length > 0} title="Run your first job" copy="Pick an agent, fill in the required fields, and choose sync (wait for result) or async (queue and poll)." actionTo="/agents" actionLabel={jobs.length > 0 ? 'Run another' : 'First job'} />
                    <ActionStep done={jobs.length > 0} title="Check on your jobs" copy="See pending, running, and completed jobs in one list." actionTo="/jobs" actionLabel="Open jobs" />
                    <ActionStep done={hasBalance} title="Keep your wallet funded" copy="Every charge, refund, and payout shows up in your wallet transactions." actionTo="/wallet" actionLabel={hasBalance ? 'View wallet' : 'Add funds'} />
                  </>
                )}
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

            <Reveal delay={0.2}>
              <Card>
                <Card.Header>
                  <span className="dashboard__section-title">How payment works</span>
                </Card.Header>
                <Card.Body className="dashboard__trust">
                  <p>We charge your wallet before a call runs. If the agent succeeds, the payout happens automatically. If it fails, you get a full refund. Every movement shows up in your ledger.</p>
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
