// OWNS: logged-in landing page. One statement-of-intent + KPIs + recent jobs + collapsed setup checklist.
// NOT OWNS: catalog, job detail, listing flow.
//
// DECISIONS:
// - One primary CTA + one secondary CTA above the fold (was 5-6 equal-weight buttons before the distill).
//   Critique P1: a new buyer must see exactly one next step, not seven.
// - The setup checklist is collapsed by default. New users with zero jobs and zero balance see it open.
// - Builder branch reads "callers hired you" framing in JobRow so a publisher doesn't see buyer-shaped rows.
import { useEffect, useMemo, useState } from 'react'
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
import { Wallet, Bot, Briefcase, CheckCircle2, ChevronDown, Terminal } from 'lucide-react'
import './DashboardPage.css'
import { fmtDate, fmtUsd } from '../utils/format.js'

const STARTER_CREDIT_LABEL = '$2.00'

function JobRow({ job, agents, role }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  const builderFraming = role === 'builder'
  const partyLabel = builderFraming ? 'Caller hire' : agent?.name ?? 'Unknown agent'
  const detail = builderFraming
    ? (agent?.name ? `via ${agent.name}` : job.job_id.slice(0, 12) + '…')
    : job.job_id.slice(0, 12) + '…'
  return (
    <Link to={`/jobs/${job.job_id}`} className="dashboard__job-row">
      <div>
        <p className="dashboard__job-name">{partyLabel}</p>
        <p className="dashboard__job-id t-mono">{detail}</p>
      </div>
      <Badge label={job.status} dot />
      <p className="dashboard__job-date">{fmtDate(job.created_at)}</p>
    </Link>
  )
}

function SetupChecklist({ role, agents, jobs, hasBalance }) {
  const items = role === 'builder'
    ? [
        { done: false, title: 'List your first agent', copy: 'Register an HTTP endpoint or upload a SKILL.md. Aztea handles billing and delivery.', to: '/list-skill', label: 'List an agent' },
        { done: false, title: 'Connect Stripe to withdraw', copy: 'Payouts land in your Aztea wallet after every successful call. Connect a bank account to cash out.', to: '/wallet', label: 'Connect Stripe' },
        { done: false, title: 'Create a scoped API key', copy: 'One key per integration so a leak only affects one surface, not your whole account.', to: '/keys', label: 'Manage keys' },
      ]
    : [
        { done: agents.length > 0, title: 'Browse the catalog', copy: 'Each listing shows what the specialist does, what it costs, and example outputs.', to: '/agents', label: agents.length > 0 ? 'Open catalog' : 'Start here' },
        { done: jobs.length > 0,   title: 'Hire your first specialist', copy: 'Pending, running, and settled jobs appear in one timeline with signed receipts.', to: '/jobs', label: 'Open jobs' },
        { done: hasBalance,        title: 'Fund the wallet', copy: 'Charges debit at hire time, refund automatically on failure. Every cent is journalled.', to: '/wallet', label: hasBalance ? 'View wallet' : 'Add funds' },
      ]

  return (
    <ul className="dashboard__steps">
      {items.map((step) => (
        <li key={step.title}>
          <Link to={step.to} className="dashboard__step">
            <div>
              <p className="dashboard__step-title">{step.title}</p>
              <p className="dashboard__step-copy">{step.copy}</p>
            </div>
            <span className={`btn btn--${step.done ? 'ghost' : 'primary'} btn--sm`} aria-hidden="true">
              {step.label}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  )
}

function buildPrimaryCta({ role, isNewUser, postAuthAgent }) {
  if (postAuthAgent) {
    return { to: `/agents/${postAuthAgent.agent_id}`, label: `Continue to ${postAuthAgent.name}`, onClick: () => { try { sessionStorage.removeItem('aztea_post_auth_agent') } catch {} } }
  }
  if (role === 'builder') {
    return { to: '/list-skill', label: isNewUser ? 'List your first agent' : 'List an agent' }
  }
  return { to: '/agents', label: isNewUser ? 'Hire your first specialist' : 'Browse the catalog' }
}

export default function DashboardPage() {
  const { agents, jobs, wallet, loading, apiKey } = useMarket()
  const { user } = useAuth()
  const [reconRuns, setReconRuns] = useState(null)
  const [setupOpen, setSetupOpen] = useState(false)

  const role = user?.role ?? 'both'
  const completedJobs = jobs.filter(j => j.status === 'complete').length
  const activeJobs = jobs.filter(j => j.status === 'running' || j.status === 'pending').length
  const successRate = jobs.length > 0 ? Math.round((completedJobs / jobs.length) * 100) : 0
  const recentJobs = jobs.slice(0, 8)
  const hasBalance = (wallet?.balance_cents ?? 0) > 0
  const isNewUser = !loading && jobs.length === 0 && !hasBalance
  const isAdmin = (user?.scopes ?? []).includes('admin')
  const balance = loading ? '…' : fmtUsd(wallet?.balance_cents ?? 0)

  const postAuthAgentId = useMemo(() => {
    try { return sessionStorage.getItem('aztea_post_auth_agent') } catch { return null }
  }, [])
  const postAuthAgent = postAuthAgentId ? agents.find(a => a.agent_id === postAuthAgentId) : null
  const primaryCta = buildPrimaryCta({ role, isNewUser, postAuthAgent })

  useEffect(() => {
    if (isNewUser) setSetupOpen(true)
  }, [isNewUser])

  useEffect(() => {
    if (!isAdmin || !apiKey) {
      setReconRuns(null)
      return
    }
    let active = true
    fetchReconciliationRuns(apiKey, 5)
      .then(data => { if (active) setReconRuns(data?.runs ?? []) })
      .catch(() => { if (active) setReconRuns([]) })
    return () => { active = false }
  }, [isAdmin, apiKey])

  const balanceHint = role === 'builder'
    ? 'Earnings · withdraw via Stripe Connect'
    : isNewUser
      ? `${STARTER_CREDIT_LABEL} starter credit`
      : 'Spend in cents · refunds on failure'

  const kpis = [
    { label: role === 'builder' ? 'Earnings balance' : 'Wallet balance', value: balance, hint: balanceHint, icon: Wallet, primary: true },
    { label: 'Specialists online', value: loading ? '…' : agents.length, hint: 'In the catalog', icon: Bot },
    { label: 'Active jobs',  value: loading ? '…' : activeJobs, hint: 'In flight or claimed', icon: Briefcase },
    { label: 'Success rate', value: loading ? '…' : `${successRate}%`, hint: jobs.length > 0 ? 'Across all your hires' : 'No jobs yet', icon: CheckCircle2 },
  ]

  return (
    <main className="dashboard">
      <Topbar crumbs={[{ label: 'Overview' }]} />

      <div className="dashboard__scroll">
        <div className="dashboard__content">

          {/* Statement of intent — one block, one primary, one secondary. */}
          <Reveal>
            <section className="dashboard__intent">
              <div className="dashboard__intent-copy">
                <p className="dashboard__intent-eyebrow t-micro">Overview</p>
                <h1 className="dashboard__intent-title">
                  {isNewUser
                    ? <>Welcome{user?.username ? `, ${user.username}` : ''}. Your agent can hire its first specialist.</>
                    : <>Welcome back{user?.username ? `, ${user.username}` : ''}.</>}
                </h1>
                <p className="dashboard__intent-sub">
                  {isNewUser && role !== 'builder'
                    ? `You have ${STARTER_CREDIT_LABEL} of starter credit. Spend it on a dependency audit, a sandboxed code run, or a live endpoint check — escrow refunds automatically on failure.`
                    : role === 'builder'
                      ? 'Publish a specialist, watch callers hire you, and read every charge, payout, and refund from one ledger.'
                      : 'Hire specialists, track jobs, and read every charge, refund, and payout from one ledger.'}
                </p>
              </div>
              <div className="dashboard__intent-actions">
                <Link to={primaryCta.to} onClick={primaryCta.onClick}>
                  <Button variant="primary">{primaryCta.label}</Button>
                </Link>
                <Link to="/docs/quickstart">
                  <Button variant="secondary" icon={<Terminal size={14} />}>Connect a coding agent</Button>
                </Link>
              </div>
            </section>
          </Reveal>

          {/* KPI strip */}
          <Stagger className="dashboard__kpi-grid" staggerDelay={0.07}>
            {kpis.map(s => (
              <div key={s.label} className={`dashboard__kpi${s.primary ? ' dashboard__kpi--primary' : ''}`}>
                <div className="dashboard__kpi-icon"><s.icon size={16} /></div>
                <p className="dashboard__kpi-value">{s.value}</p>
                <p className="dashboard__kpi-label">{s.label}</p>
                {s.hint && <p className="dashboard__kpi-hint">{s.hint}</p>}
              </div>
            ))}
          </Stagger>

          {isAdmin && reconRuns && reconRuns.length > 0 && (
            <Reveal delay={0.08}>
              <Card>
                <Card.Header><span className="dashboard__section-title">Ledger reconciliation</span></Card.Header>
                <Card.Body>
                  <p className="dashboard__kpi-hint">
                    Latest: {fmtDate(reconRuns[0]?.created_at)} · drift {(reconRuns[0]?.drift_cents ?? 0)}¢ · mismatches {(reconRuns[0]?.mismatch_count ?? 0)}
                  </p>
                  <div className="dashboard__trust-pills">
                    <Badge label={reconRuns[0]?.invariant_ok ? 'invariant_ok' : 'invariant_failed'} dot />
                  </div>
                </Card.Body>
              </Card>
            </Reveal>
          )}

          {/* Setup — collapsed by default unless new user */}
          <Reveal delay={0.1}>
            <details className="dashboard__setup" open={setupOpen} onToggle={(e) => setSetupOpen(e.currentTarget.open)}>
              <summary className="dashboard__setup-summary">
                <span className="dashboard__section-title">Setup</span>
                <ChevronDown size={14} className="dashboard__setup-chev" />
              </summary>
              <div className="dashboard__setup-body">
                <SetupChecklist role={role} agents={agents} jobs={jobs} hasBalance={hasBalance} />
              </div>
            </details>
          </Reveal>

          {/* Recent jobs */}
          <Reveal delay={0.15}>
            <Card>
              <Card.Header className="dashboard__panel-head">
                <span className="dashboard__section-title">{role === 'builder' ? 'Recent caller hires' : 'Recent jobs'}</span>
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
                    title={role === 'builder' ? 'No caller hires yet' : 'No jobs yet'}
                    sub={role === 'builder' ? 'List a specialist and your first hire will appear here.' : 'Choose an agent from the catalog and run your first job.'}
                    action={<Link to={primaryCta.to}><Button variant="primary">{primaryCta.label}</Button></Link>}
                  />
                ) : (
                  <div className="dashboard__jobs">
                    {recentJobs.map(job => <JobRow key={job.job_id} job={job} agents={agents} role={role} />)}
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
