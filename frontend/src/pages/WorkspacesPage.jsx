import { useEffect, useState, useMemo } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import { ShieldCheck, ShieldX, ShieldQuestion, RefreshCcw, Trash2 } from 'lucide-react'
import Topbar from '../layout/Topbar'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Button from '../ui/Button'
import Badge from '../ui/Badge'
import Reveal from '../ui/motion/Reveal'
import {
  fetchWorkspaceList,
  fetchWorkspaceArtifacts,
  fetchWorkspaceManifest,
  verifyWorkspaceSeal,
  deleteWorkspace,
} from '../api'
import { useAuth } from '../context/AuthContext'
import { fmtDate, fmtUsd } from '../utils/format.js'
import './WorkspacesPage.css'

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

function fmtBytes(n) {
  if (n == null) return '—'
  const KB = 1024, MB = KB * 1024
  if (n >= MB) return `${(n / MB).toFixed(1)} MB`
  if (n >= KB) return `${(n / KB).toFixed(1)} KB`
  return `${n} B`
}

const STATUS_LABEL = {
  active: 'Active',
  sealed: 'Sealed',
  expired: 'Expired',
  sandbox_evicted: 'Sandbox lost',
}

const STATUS_BADGE_STYLE = {
  active: { color: '#16a34a', bg: '#dcfce7' },
  sealed: { color: '#1d4ed8', bg: '#dbeafe' },
  expired: { color: '#737373', bg: '#f5f5f5' },
  sandbox_evicted: { color: '#b91c1c', bg: '#fee2e2' },
}

// ---------------------------------------------------------------------------
// Row + detail
// ---------------------------------------------------------------------------

function WorkspaceRow({ ws, expanded, onToggle }) {
  const style = STATUS_BADGE_STYLE[ws.status] || STATUS_BADGE_STYLE.active
  return (
    <button
      type="button"
      className={`workspaces__row${expanded ? ' workspaces__row--expanded' : ''}`}
      onClick={onToggle}
    >
      <div className="workspaces__row-main">
        <div className="workspaces__row-id t-mono" title={ws.workspace_id}>
          {ws.workspace_id.slice(0, 16)}…
        </div>
        {ws.run_id && (
          <div className="workspaces__row-runlink t-mono" title={ws.run_id}>
            run {ws.run_id.slice(0, 10)}…
          </div>
        )}
      </div>
      <span
        className="workspaces__status"
        style={{ color: style.color, backgroundColor: style.bg }}
      >
        {STATUS_LABEL[ws.status] || ws.status}
      </span>
      <span className="workspaces__artifacts t-mono">{ws.artifact_count} files</span>
      <span className="workspaces__bytes t-mono">{fmtBytes(ws.total_bytes)}</span>
      <span className="workspaces__date">{fmtDate(ws.sealed_at || ws.created_at)}</span>
    </button>
  )
}

function WorkspaceDetail({ ws, apiKey, onDeleted }) {
  const [artifacts, setArtifacts] = useState(null)
  const [manifest, setManifest] = useState(null)
  const [verify, setVerify] = useState(null) // {valid, signer_did, sealed_at} | 'loading' | {error}
  const [showManifest, setShowManifest] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    fetchWorkspaceArtifacts(apiKey, ws.workspace_id)
      .then(body => { if (!cancelled) setArtifacts(body.artifacts || []) })
      .catch(err => { if (!cancelled) setError(String(err?.message || err)) })
    if (ws.status === 'sealed') {
      fetchWorkspaceManifest(ws.workspace_id)
        .then(body => { if (!cancelled) setManifest(body) })
        .catch(() => { /* sealed but not yet fetchable; ignore */ })
    }
    return () => { cancelled = true }
  }, [apiKey, ws.workspace_id, ws.status])

  const handleVerify = async () => {
    setVerify('loading')
    try {
      const result = await verifyWorkspaceSeal(ws.workspace_id)
      setVerify(result)
    } catch (err) {
      setVerify({ error: String(err?.message || err) })
    }
  }

  const handleDelete = async () => {
    if (!confirm(`Delete workspace ${ws.workspace_id}? This cannot be undone.`)) return
    setDeleting(true)
    try {
      await deleteWorkspace(apiKey, ws.workspace_id)
      onDeleted(ws.workspace_id)
    } catch (err) {
      setError(String(err?.message || err))
      setDeleting(false)
    }
  }

  return (
    <motion.div
      className="workspaces__detail"
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.18 }}
    >
      <div className="workspaces__detail-meta">
        <div>
          <div className="workspaces__detail-label">Workspace ID</div>
          <div className="workspaces__detail-value t-mono">{ws.workspace_id}</div>
        </div>
        <div>
          <div className="workspaces__detail-label">Backing</div>
          <div className="workspaces__detail-value t-mono">
            {ws.backing_type}
            {ws.backing_id ? ` · ${ws.backing_id}` : ''}
          </div>
        </div>
        <div>
          <div className="workspaces__detail-label">Quota</div>
          <div className="workspaces__detail-value t-mono">
            {fmtBytes(ws.total_bytes)} / {fmtBytes(ws.quota_bytes)}
          </div>
        </div>
        <div>
          <div className="workspaces__detail-label">Created</div>
          <div className="workspaces__detail-value">{fmtDate(ws.created_at)}</div>
        </div>
        <div>
          <div className="workspaces__detail-label">Expires</div>
          <div className="workspaces__detail-value">{fmtDate(ws.expires_at)}</div>
        </div>
        {ws.content_purged_at && (
          <div>
            <div className="workspaces__detail-label">Content purged</div>
            <div className="workspaces__detail-value">
              {fmtDate(ws.content_purged_at)}
              <span className="workspaces__purged-hint">
                Metadata + manifest retained; bytes are gone.
              </span>
            </div>
          </div>
        )}
      </div>

      {error && <div className="workspaces__error">{error}</div>}

      <div className="workspaces__artifacts-list">
        <div className="workspaces__detail-section-label">Artifacts</div>
        {artifacts === null && <Skeleton height={48} />}
        {artifacts !== null && artifacts.length === 0 && (
          <div className="workspaces__empty-inline">No artifacts yet.</div>
        )}
        {artifacts !== null && artifacts.length > 0 && (
          <table className="workspaces__artifacts-table">
            <thead>
              <tr>
                <th>Name</th>
                <th>Size</th>
                <th>sha256</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {artifacts.map(a => (
                <tr key={a.name}>
                  <td className="t-mono">{a.name}</td>
                  <td className="t-mono">{fmtBytes(a.size_bytes)}</td>
                  <td
                    className="t-mono workspaces__sha"
                    title={a.sha256}
                  >
                    {a.sha256.slice(0, 12)}…
                  </td>
                  <td>{fmtDate(a.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {ws.status === 'sealed' && (
        <div className="workspaces__seal">
          <div className="workspaces__detail-section-label">Signed manifest</div>
          <div className="workspaces__seal-row">
            <Button
              size="small"
              variant="ghost"
              icon={
                verify === 'loading' ? <RefreshCcw size={14} /> :
                verify?.valid === true ? <ShieldCheck size={14} /> :
                verify?.valid === false ? <ShieldX size={14} /> :
                <ShieldQuestion size={14} />
              }
              onClick={handleVerify}
              disabled={verify === 'loading'}
            >
              {verify === 'loading' ? 'Verifying…' :
                verify?.valid === true ? 'Manifest verified' :
                verify?.valid === false ? 'Manifest INVALID' :
                'Verify manifest'}
            </Button>
            <span className="workspaces__signer t-mono" title={manifest?.public_key_did || ws.seal_public_key_did}>
              {(manifest?.public_key_did || ws.seal_public_key_did || '').slice(0, 48)}…
            </span>
          </div>
          {verify?.error && (
            <div className="workspaces__error">Verify failed: {verify.error}</div>
          )}
          {manifest && (
            <div className="workspaces__manifest">
              <button
                type="button"
                className="workspaces__manifest-toggle"
                onClick={() => setShowManifest(s => !s)}
              >
                {showManifest ? 'Hide' : 'Show'} raw manifest (JSON)
              </button>
              {showManifest && (
                <pre className="workspaces__manifest-body">
                  {JSON.stringify(manifest, null, 2)}
                </pre>
              )}
            </div>
          )}
        </div>
      )}

      <div className="workspaces__actions">
        <Button
          size="small"
          variant="ghost"
          icon={<Trash2 size={14} />}
          onClick={handleDelete}
          disabled={deleting}
        >
          {deleting ? 'Deleting…' : 'Delete workspace'}
        </Button>
      </div>
    </motion.div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

const TABS = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'sealed', label: 'Sealed' },
  { id: 'expired', label: 'Expired' },
]

export default function WorkspacesPage() {
  const { apiKey } = useAuth()
  const [workspaces, setWorkspaces] = useState(null)
  const [error, setError] = useState(null)
  const [tab, setTab] = useState('all')
  const [expandedId, setExpandedId] = useState(null)

  const loadList = async () => {
    setError(null)
    try {
      const body = await fetchWorkspaceList(apiKey, 100)
      setWorkspaces(body.workspaces || [])
    } catch (err) {
      setError(String(err?.message || err))
    }
  }

  useEffect(() => {
    if (apiKey) loadList()
  }, [apiKey])

  const filtered = useMemo(() => {
    if (workspaces == null) return null
    if (tab === 'all') return workspaces
    return workspaces.filter(w => w.status === tab)
  }, [workspaces, tab])

  const handleDeleted = (workspaceId) => {
    setWorkspaces(prev => (prev || []).filter(w => w.workspace_id !== workspaceId))
    if (expandedId === workspaceId) setExpandedId(null)
  }

  return (
    <div className="workspaces">
      <Topbar
        title="Workspaces"
        subtitle="Shared artifact stores for multi-agent workflows. Each can be sealed to produce a signed audit manifest."
      />
      <div className="workspaces__scroll">
        <div className="workspaces__content">
          <div className="workspaces__header">
            <div className="workspaces__tabs">
              {TABS.map(t => (
                <button
                  key={t.id}
                  type="button"
                  className={`workspaces__tab${tab === t.id ? ' workspaces__tab--active' : ''}`}
                  onClick={() => setTab(t.id)}
                >
                  {t.label}
                </button>
              ))}
            </div>
            <Button size="small" variant="ghost" icon={<RefreshCcw size={14} />} onClick={loadList}>
              Refresh
            </Button>
          </div>

          {error && <div className="workspaces__error">{error}</div>}

          {workspaces === null && (
            <div className="workspaces__loading">
              <Skeleton height={56} />
              <Skeleton height={56} />
              <Skeleton height={56} />
            </div>
          )}

          {filtered !== null && filtered.length === 0 && (
            <EmptyState
              title={tab === 'all' ? 'No workspaces yet' : `No ${tab} workspaces`}
              description={
                tab === 'all'
                  ? 'Workspaces are created by recipes with auto_workspace=true, or by calling POST /workspaces directly. See the docs for the full conventions.'
                  : 'Switch tabs above or run a workflow that opts into auto_workspace.'
              }
            />
          )}

          {filtered !== null && filtered.length > 0 && (
            <div className="workspaces__list">
              {filtered.map(ws => (
                <Reveal key={ws.workspace_id} delay={0.02}>
                  <WorkspaceRow
                    ws={ws}
                    expanded={expandedId === ws.workspace_id}
                    onToggle={() =>
                      setExpandedId(prev => prev === ws.workspace_id ? null : ws.workspace_id)
                    }
                  />
                  <AnimatePresence>
                    {expandedId === ws.workspace_id && (
                      <WorkspaceDetail
                        ws={ws}
                        apiKey={apiKey}
                        onDeleted={handleDeleted}
                      />
                    )}
                  </AnimatePresence>
                </Reveal>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
