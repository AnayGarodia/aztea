import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Stat from '../ui/Stat'
import { useMarket } from '../context/MarketContext'

function fmtUsd(cents) {
  if (typeof cents !== 'number') return '--'
  return `$${(cents / 100).toFixed(2)}`
}

export default function DashboardPage() {
  const { agents, jobs, wallet, runs } = useMarket()

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ label: 'Overview' }]} />
      <section style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 12 }}>
        <Stat label="Agents" value={agents.length} />
        <Stat label="Jobs" value={jobs.length} />
        <Stat label="Runs logged" value={runs.length} />
        <Stat label="Wallet" value={fmtUsd(wallet?.balance_cents)} />
      </section>
      <Card title="Recent jobs">
        {jobs.length === 0 ? (
          <p style={{ color: 'var(--ink-mute)' }}>No jobs yet.</p>
        ) : (
          <div style={{ display: 'grid', gap: 8 }}>
            {jobs.slice(0, 8).map((job) => (
              <div key={job.job_id} style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
                <span>{job.job_id}</span>
                <span style={{ color: 'var(--ink-mute)' }}>{job.status}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </main>
  )
}
