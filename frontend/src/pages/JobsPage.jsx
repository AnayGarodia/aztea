import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import EmptyState from '../ui/EmptyState'
import { useMarket } from '../context/MarketContext'

export default function JobsPage() {
  const { jobs } = useMarket()

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ label: 'Jobs' }]} />
      {jobs.length === 0 ? (
        <EmptyState title="No jobs yet" sub="Queued jobs will appear here." />
      ) : (
        <Card>
          <Card.Header>
            <strong>Recent jobs</strong>
          </Card.Header>
          <Card.Body>
            <div style={{ display: 'grid', gap: 10 }}>
              {jobs.map((job) => (
                <Link
                  key={job.job_id}
                  to={`/jobs/${job.job_id}`}
                  style={{ display: 'flex', justifyContent: 'space-between', textDecoration: 'none', color: 'inherit' }}
                >
                  <span>{job.job_id}</span>
                  <span style={{ color: 'var(--ink-mute)' }}>{job.status}</span>
                </Link>
              ))}
            </div>
          </Card.Body>
        </Card>
      )}
    </main>
  )
}
