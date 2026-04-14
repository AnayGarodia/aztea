import { useState, useMemo } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import { useMarket } from '../context/MarketContext'
import { ChevronRight } from 'lucide-react'

const TABS = [
  { id: 'all',       label: 'All' },
  { id: 'running',   label: 'Running' },
  { id: 'pending',   label: 'Pending' },
  { id: 'complete',  label: 'Complete' },
  { id: 'failed',    label: 'Failed' },
]

function fmtUsd(cents) {
  if (typeof cents !== 'number') return null
  return '$' + (cents / 100).toFixed(2)
}

function fmtDate(str) {
  if (!str) return '--'
  const d = new Date(str)
  const now = new Date()
  const diffMs = now - d
  if (diffMs < 60_000) return 'just now'
  if (diffMs < 3_600_000) return `${Math.floor(diffMs / 60_000)}m ago`
  if (diffMs < 86_400_000) return `${Math.floor(diffMs / 3_600_000)}h ago`
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function JobRow({ job, agents }) {
  const agent = agents.find(a => a.agent_id === job.agent_id)
  const price = fmtUsd(job.price_cents)

  return (
    <Link
      to={`/jobs/${job.job_id}`}
      style={{
        display: 'grid',
        gridTemplateColumns: '1fr auto auto auto auto',
        gap: 'var(--sp-4)',
        alignItems: 'center',
        padding: '13px var(--sp-5)',
        borderBottom: '1px solid var(--line)',
        textDecoration: 'none',
        color: 'inherit',
        transition: 'background var(--duration-sm) var(--ease)',
      }}
      onMouseEnter={e => { e.currentTarget.style.background = 'var(--canvas-sunk)' }}
      onMouseLeave={e => { e.currentTarget.style.background = '' }}
    >
      {/* Agent + ID */}
      <div style={{ minWidth: 0 }}>
        <p style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--ink)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {agent?.name ?? <span style={{ color: 'var(--ink-mute)' }}>Unknown agent</span>}
        </p>
        <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', fontFamily: 'var(--font-mono)' }}>
          {job.job_id.slice(0, 12)}…
        </p>
      </div>

      {/* Status */}
      <Badge label={job.status} dot />

      {/* Price */}
      {price ? (
        <span style={{ fontSize: '0.8125rem', fontFamily: 'var(--font-mono)', color: 'var(--ink-soft)', fontFeatureSettings: '"tnum"', whiteSpace: 'nowrap' }}>
          {price}
        </span>
      ) : <span />}

      {/* Date */}
      <span style={{ fontSize: '0.8125rem', color: 'var(--ink-mute)', whiteSpace: 'nowrap' }}>
        {fmtDate(job.created_at)}
      </span>

      {/* Arrow */}
      <ChevronRight size={15} color="var(--ink-faint)" />
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

  // Count per tab
  const counts = useMemo(() => {
    const c = {}
    TABS.forEach(t => {
      c[t.id] = t.id === 'all' ? jobs.length : jobs.filter(j => j.status === t.id).length
    })
    return c
  }, [jobs])

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ label: 'Jobs' }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Header */}
        <div style={{ marginBottom: 'var(--sp-5)' }}>
          <h1 style={{
            fontFamily: 'var(--font-display)',
            fontSize: '1.625rem', fontWeight: 400,
            color: 'var(--ink)', letterSpacing: '-0.02em', lineHeight: 1.2,
            marginBottom: 4,
          }}>
            Jobs
          </h1>
          <p style={{ fontSize: '0.875rem', color: 'var(--ink-mute)' }}>
            {loading ? '…' : `${jobs.length} job${jobs.length !== 1 ? 's' : ''} total`}
          </p>
        </div>

        {/* Tabs */}
        <div style={{
          display: 'flex', gap: 2, borderBottom: '1px solid var(--line)',
          marginBottom: 'var(--sp-4)',
        }}>
          {TABS.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: 6,
                padding: '9px 14px',
                fontSize: '0.875rem', fontWeight: 500,
                color: activeTab === tab.id ? 'var(--ink)' : 'var(--ink-mute)',
                marginBottom: -1,
                cursor: 'pointer',
                background: 'none',
                borderBottom: activeTab === tab.id ? '2px solid var(--accent)' : '2px solid transparent',
                transition: 'color var(--duration-sm) var(--ease)',
                whiteSpace: 'nowrap',
              }}
            >
              {tab.label}
              {counts[tab.id] > 0 && (
                <span style={{
                  fontSize: '0.6875rem', fontWeight: 600,
                  background: activeTab === tab.id ? 'var(--accent-wash)' : 'var(--canvas-sunk)',
                  color: activeTab === tab.id ? 'var(--accent)' : 'var(--ink-mute)',
                  border: '1px solid',
                  borderColor: activeTab === tab.id ? 'var(--accent-line)' : 'var(--line)',
                  borderRadius: 'var(--r-pill)',
                  padding: '1px 6px',
                  fontFamily: 'var(--font-mono)',
                  fontFeatureSettings: '"tnum"',
                }}>
                  {counts[tab.id]}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Table */}
        {loading ? (
          <Card>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-2)', padding: 'var(--sp-3)' }}>
              {[1, 2, 3, 4, 5].map(i => <Skeleton key={i} variant="rect" height={60} />)}
            </div>
          </Card>
        ) : filtered.length === 0 ? (
          <EmptyState
            title={activeTab === 'all' ? 'No jobs yet' : `No ${activeTab} jobs`}
            sub={activeTab === 'all' ? 'Hire an agent to create your first job.' : 'Jobs in this state will appear here.'}
          />
        ) : (
          <Card variant="flat" style={{ overflow: 'hidden' }}>
            {/* Table header */}
            <div style={{
              display: 'grid',
              gridTemplateColumns: '1fr auto auto auto auto',
              gap: 'var(--sp-4)',
              padding: '9px var(--sp-5)',
              background: 'var(--canvas-sunk)',
              borderBottom: '1px solid var(--line)',
            }}>
              {['Agent / Job ID', 'Status', 'Cost', 'Created', ''].map((h, i) => (
                <span key={i} style={{
                  fontSize: '0.6875rem', fontWeight: 600,
                  letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--ink-mute)',
                }}>
                  {h}
                </span>
              ))}
            </div>
            {filtered.map(job => (
              <JobRow key={job.job_id} job={job} agents={agents} />
            ))}
          </Card>
        )}
      </div>
    </main>
  )
}
