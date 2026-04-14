import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Stat from '../ui/Stat'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import { useMarket } from '../context/MarketContext'
import { ArrowUpRight } from 'lucide-react'

function fmtUsd(cents) {
  if (typeof cents !== 'number') return '--'
  return '$' + (cents / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function fmtDate(str) {
  if (!str) return '--'
  return new Date(str).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function JobRow({ job, agents }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  return (
    <Link
      to={`/jobs/${job.job_id}`}
      style={{
        display: 'grid',
        gridTemplateColumns: '1fr auto auto auto',
        gap: 'var(--sp-4)',
        alignItems: 'center',
        padding: '11px 0',
        borderBottom: '1px solid var(--line)',
        textDecoration: 'none',
        color: 'inherit',
      }}
    >
      <div style={{ minWidth: 0 }}>
        <p style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--ink)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {agent?.name ?? 'Unknown agent'}
        </p>
        <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', fontFamily: 'var(--font-mono)' }}>
          {job.job_id.slice(0, 8)}…
        </p>
      </div>
      <Badge label={job.status} dot />
      {job.price_cents != null && (
        <span style={{ fontSize: '0.8125rem', fontFamily: 'var(--font-mono)', color: 'var(--ink-soft)', fontFeatureSettings: '"tnum"', whiteSpace: 'nowrap' }}>
          {fmtUsd(job.price_cents)}
        </span>
      )}
      <span style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', whiteSpace: 'nowrap' }}>
        {fmtDate(job.created_at)}
      </span>
    </Link>
  )
}

function AgentQuickLink({ agent }) {
  return (
    <Link
      to={`/agents/${agent.agent_id}`}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 'var(--sp-3)',
        padding: 'var(--sp-3) var(--sp-4)',
        border: '1px solid var(--line)',
        borderRadius: 'var(--r-sm)',
        textDecoration: 'none',
        color: 'inherit',
        transition: 'background var(--duration-sm) var(--ease), border-color var(--duration-sm) var(--ease)',
        background: 'var(--surface)',
      }}
      onMouseEnter={e => { e.currentTarget.style.background = 'var(--surface-hover)'; e.currentTarget.style.borderColor = 'var(--line-strong)' }}
      onMouseLeave={e => { e.currentTarget.style.background = 'var(--surface)'; e.currentTarget.style.borderColor = 'var(--line)' }}
    >
      <div style={{ minWidth: 0 }}>
        <p style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--ink)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {agent.name}
        </p>
        <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', fontFamily: 'var(--font-mono)', fontFeatureSettings: '"tnum"' }}>
          ${Number(agent.price_per_call_usd).toFixed(2)} / call
        </p>
      </div>
      <ArrowUpRight size={14} color="var(--ink-faint)" style={{ flexShrink: 0 }} />
    </Link>
  )
}

export default function DashboardPage() {
  const { agents, jobs, wallet, loading } = useMarket()

  const completedJobs = jobs.filter(j => j.status === 'complete').length
  const activeJobs = jobs.filter(j => j.status === 'running' || j.status === 'pending').length
  const successRate = jobs.length > 0 ? Math.round((completedJobs / jobs.length) * 100) : null
  const recentJobs = jobs.slice(0, 8)

  const stats = [
    { label: 'Agents', value: loading ? '…' : agents.length },
    { label: 'Total jobs', value: loading ? '…' : jobs.length },
    { label: 'Active now', value: loading ? '…' : activeJobs, variant: activeJobs > 0 ? 'accent' : '' },
    ...(successRate != null ? [{ label: 'Success rate', value: `${successRate}%`, variant: 'positive' }] : []),
  ]

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ label: 'Overview' }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Balance hero */}
        <div style={{ marginBottom: 'var(--sp-7)' }}>
          <p style={{
            fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.07em',
            textTransform: 'uppercase', color: 'var(--ink-mute)', marginBottom: 'var(--sp-2)',
          }}>
            Wallet balance
          </p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-5)', flexWrap: 'wrap' }}>
            <p style={{
              fontFamily: 'var(--font-mono)',
              fontSize: 'clamp(2.25rem, 5vw, 3.25rem)',
              fontWeight: 500,
              color: 'var(--ink)',
              letterSpacing: '-0.025em',
              fontFeatureSettings: '"tnum"',
              lineHeight: 1,
            }}>
              {loading ? '—' : fmtUsd(wallet?.balance_cents)}
            </p>
            <Link to="/wallet">
              <Button variant="secondary" size="sm" iconRight={<ArrowUpRight size={13} />}>
                Add funds
              </Button>
            </Link>
          </div>
        </div>

        {/* Stats strip */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: `repeat(${stats.length}, 1fr)`,
          gap: 'var(--sp-3)',
          marginBottom: 'var(--sp-6)',
        }}>
          {stats.map(s => (
            <div key={s.label} style={{
              padding: 'var(--sp-4)',
              background: 'var(--surface)',
              border: '1px solid var(--line)',
              borderRadius: 'var(--r-md)',
              boxShadow: 'var(--shadow-xs)',
            }}>
              <Stat label={s.label} value={s.value} variant={s.variant} />
            </div>
          ))}
        </div>

        {/* Main content */}
        <div style={{
          display: 'grid',
          gridTemplateColumns: '1fr minmax(220px, 300px)',
          gap: 'var(--sp-5)',
          alignItems: 'start',
        }}>

          {/* Recent jobs */}
          <Card>
            <Card.Header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem', color: 'var(--ink)' }}>Recent jobs</span>
              <Link to="/jobs">
                <Button variant="ghost" size="sm">View all</Button>
              </Link>
            </Card.Header>
            <Card.Body>
              {loading ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-3)' }}>
                  {[1, 2, 3, 4].map(i => <Skeleton key={i} variant="rect" height={52} />)}
                </div>
              ) : recentJobs.length === 0 ? (
                <EmptyState
                  title="No jobs yet"
                  sub={
                    <span>
                      Go to{' '}
                      <Link to="/agents" style={{ color: 'var(--accent)', textDecoration: 'underline' }}>
                        Agents
                      </Link>
                      {' '}to hire your first agent.
                    </span>
                  }
                />
              ) : (
                <div>
                  {recentJobs.map(job => (
                    <JobRow key={job.job_id} job={job} agents={agents} />
                  ))}
                </div>
              )}
            </Card.Body>
          </Card>

          {/* Quick hire */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem', color: 'var(--ink)' }}>Hire an agent</span>
            </Card.Header>
            <Card.Body style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)' }}>
              {loading ? (
                [1, 2, 3].map(i => <Skeleton key={i} variant="rect" height={56} />)
              ) : agents.length === 0 ? (
                <EmptyState title="No agents" sub="Register an agent to get started." />
              ) : (
                <>
                  {agents.slice(0, 4).map(agent => (
                    <AgentQuickLink key={agent.agent_id} agent={agent} />
                  ))}
                  {agents.length > 4 && (
                    <Link to="/agents" style={{ display: 'block', marginTop: 'var(--sp-2)' }}>
                      <Button variant="ghost" size="sm" style={{ width: '100%' }}>
                        View all {agents.length} agents
                      </Button>
                    </Link>
                  )}
                </>
              )}
            </Card.Body>
          </Card>

        </div>
      </div>
    </main>
  )
}
