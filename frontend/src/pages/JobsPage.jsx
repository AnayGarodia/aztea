import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Button from '../ui/Button'
import { useMarket } from '../context/MarketContext'
import './JobsPage.css'

const TABS = [
  { id: 'all', label: 'All' },
  { id: 'running', label: 'Running' },
  { id: 'pending', label: 'Pending' },
  { id: 'complete', label: 'Complete' },
  { id: 'failed', label: 'Failed' },
]

function fmtUsd(cents) {
  if (typeof cents !== 'number') return '—'
  return '$' + (cents / 100).toFixed(2)
}

function fmtDate(str) {
  if (!str) return '--'
  const d = new Date(str)
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function JobRow({ job, agents }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  return (
    <Link to={`/jobs/${job.job_id}`} className="jobs__row">
      <p className="jobs__agent">{agent?.name ?? 'Unknown agent'}</p>
      <p className={`jobs__status jobs__status--${job.status}`}>{job.status}</p>
      <p className="jobs__cost">{fmtUsd(job.price_cents)}</p>
      <p className="jobs__created">{fmtDate(job.created_at)}</p>
    </Link>
  )
}

export default function JobsPage() {
  const { jobs, agents, loading } = useMarket()
  const [activeTab, setActiveTab] = useState('all')

  const filtered = useMemo(() => {
    if (activeTab === 'all') return jobs
    return jobs.filter(j => j.status === activeTab)
  }, [jobs, activeTab])

  const counts = useMemo(() => {
    const c = {}
    TABS.forEach(t => {
      c[t.id] = t.id === 'all' ? jobs.length : jobs.filter(j => j.status === t.id).length
    })
    return c
  }, [jobs])

  return (
    <main className="jobs">
      <Topbar crumbs={[{ label: 'Jobs' }]} />

      <div className="jobs__scroll">
        <div className="jobs__content">
          <header className="jobs__header">
            <div>
              <p className="jobs__eyebrow">Monitor work</p>
              <h1>Jobs</h1>
              <p>Track every async task from queue to completion. Click any row for payload, output, and timeline.</p>
            </div>
            <Link to="/agents">
              <Button variant="primary" size="sm">Create new job</Button>
            </Link>
          </header>

          <section className="jobs__legend">
            <p><strong>Pending:</strong> accepted, waiting to run</p>
            <p><strong>Running:</strong> currently executing</p>
            <p><strong>Complete/Failed:</strong> final output or error + settlement</p>
          </section>

          <div className="jobs__tabs" role="tablist" aria-label="Job status filters">
            {TABS.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`jobs__tab ${activeTab === tab.id ? 'jobs__tab--active' : ''}`}
                role="tab"
                aria-selected={activeTab === tab.id}
              >
                {tab.label}
                {counts[tab.id] > 0 && <span>{counts[tab.id]}</span>}
              </button>
            ))}
          </div>

          {loading ? (
            <div className="jobs__table">
              {[1, 2, 3, 4, 5].map(i => <Skeleton key={i} variant="rect" height={64} style={{ margin: '6px 16px' }} />)}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title={activeTab === 'all' ? 'No jobs yet' : `No ${activeTab} jobs`}
              sub={activeTab === 'all' ? 'Create a job by invoking an agent from the marketplace.' : 'Switch filters or create a new job.'}
              action={
                <div className="jobs__empty-actions">
                  {activeTab !== 'all' && (
                    <Button variant="secondary" onClick={() => setActiveTab('all')}>
                      Show all jobs
                    </Button>
                  )}
                  <Link to="/agents">
                    <Button variant="primary">Discover agents</Button>
                  </Link>
                </div>
              }
            />
          ) : (
            <section className="jobs__table" aria-label="Jobs table">
              <div className="jobs__head">
                <span>Agent</span>
                <span>Status</span>
                <span>Cost</span>
                <span>Created</span>
              </div>
              {filtered.map(job => (
                <JobRow key={job.job_id} job={job} agents={agents} />
              ))}
            </section>
          )}
        </div>
      </div>
    </main>
  )
}
