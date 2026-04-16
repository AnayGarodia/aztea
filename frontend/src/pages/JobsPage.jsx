import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import Topbar from '../layout/Topbar'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Reveal from '../ui/motion/Reveal'
import { useMarket } from '../context/MarketContext'
import './JobsPage.css'

const TABS = [
  { id: 'all',      label: 'All' },
  { id: 'running',  label: 'Running' },
  { id: 'pending',  label: 'Pending' },
  { id: 'complete', label: 'Complete' },
  { id: 'failed',   label: 'Failed' },
]

function fmtUsd(cents) {
  if (typeof cents !== 'number') return '—'
  return '$' + (cents / 100).toFixed(2)
}

function fmtDate(str) {
  if (!str) return '--'
  return new Date(str).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function JobRow({ job, agents }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  const isLive = job.status === 'running' || job.status === 'pending'
  return (
    <Link to={`/jobs/${job.job_id}`} className="jobs__row">
      <div className="jobs__row-main">
        <div className="jobs__row-agent-name">{agent?.name ?? 'Unknown agent'}</div>
        <div className="jobs__row-id t-mono">{job.job_id.slice(0, 12)}…</div>
      </div>
      <Badge label={job.status} dot />
      <span className="jobs__cost t-mono">{fmtUsd(job.price_cents)}</span>
      <span className="jobs__created">{fmtDate(job.created_at)}</span>
    </Link>
  )
}

export default function JobsPage() {
  const { jobs, agents, loading } = useMarket()
  const [activeTab, setActiveTab] = useState('all')

  const filtered = useMemo(() =>
    activeTab === 'all' ? jobs : jobs.filter(j => j.status === activeTab),
    [jobs, activeTab]
  )

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
          <Reveal>
            <header className="jobs__header">
              <div>
                <p className="jobs__eyebrow t-micro">Monitor work</p>
                <h1>Jobs</h1>
                <p>Track every async task from queue to completion.</p>
              </div>
              <Link to="/agents">
                <Button variant="primary" size="sm">New job</Button>
              </Link>
            </header>
          </Reveal>

          {/* Tabs */}
          <div className="jobs__tabs" role="tablist" aria-label="Job status filters">
            {TABS.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`jobs__tab ${activeTab === tab.id ? 'jobs__tab--active' : ''}`}
                role="tab"
                aria-selected={activeTab === tab.id}
              >
                {activeTab === tab.id && (
                  <motion.span
                    layoutId="jobs-tab-indicator"
                    className="jobs__tab-bg"
                    transition={{ type: 'spring', bounce: 0.2, duration: 0.35 }}
                  />
                )}
                <span style={{ position: 'relative', zIndex: 1 }}>{tab.label}</span>
                {counts[tab.id] > 0 && (
                  <span className="jobs__tab-count" style={{ position: 'relative', zIndex: 1 }}>
                    {counts[tab.id]}
                  </span>
                )}
              </button>
            ))}
          </div>

          {loading ? (
            <div className="jobs__skeleton-list">
              {[1,2,3,4,5].map(i => <Skeleton key={i} variant="rect" height={60} />)}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              agentId={`empty-jobs-${activeTab}`}
              title={activeTab === 'all' ? 'No jobs yet' : `No ${activeTab} jobs`}
              sub={activeTab === 'all' ? 'Create a job by invoking an agent from the marketplace.' : 'Switch filters or create a new job.'}
              action={
                <div className="jobs__empty-actions">
                  {activeTab !== 'all' && (
                    <Button variant="secondary" onClick={() => setActiveTab('all')}>Show all</Button>
                  )}
                  <Link to="/agents"><Button variant="primary">Discover agents</Button></Link>
                </div>
              }
            />
          ) : (
            <section className="jobs__table" aria-label="Jobs">
              <div className="jobs__head">
                <span>Agent</span>
                <span>Status</span>
                <span>Cost</span>
                <span>Created</span>
              </div>
              <AnimatePresence mode="popLayout">
                {filtered.map((job, i) => (
                  <motion.div
                    key={job.job_id}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -8 }}
                    transition={{ delay: i * 0.03, duration: 0.25 }}
                  >
                    <JobRow job={job} agents={agents} />
                  </motion.div>
                ))}
              </AnimatePresence>
            </section>
          )}
        </div>
      </div>
    </main>
  )
}
