import { useEffect, useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Avatar from '../ui/Avatar'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import { createAuthKey, deleteAuthKey, fetchAuthKeys } from '../api'
import { useMarket } from '../context/MarketContext'
import { useAuth } from '../context/AuthContext'
import { Key, Plus, Trash2, Copy } from 'lucide-react'

function fmtDate(str) {
  if (!str) return 'Never'
  return new Date(str).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

function ApiKeyRow({ item, onRevoke }) {
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: '1fr auto auto',
      gap: 'var(--sp-4)',
      alignItems: 'center',
      padding: '12px 0',
      borderBottom: '1px solid var(--line)',
    }}>
      <div style={{ minWidth: 0 }}>
        <p style={{ fontSize: '0.875rem', fontWeight: 500, color: 'var(--ink)', marginBottom: 2 }}>
          {item.name}
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-3)', flexWrap: 'wrap' }}>
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.8125rem', color: 'var(--ink-mute)' }}>
            {item.key_prefix}…
          </span>
          <span style={{ fontSize: '0.75rem', color: 'var(--ink-mute)' }}>
            Last used: {fmtDate(item.last_used_at)}
          </span>
          {(item.scopes ?? []).map(s => (
            <Badge key={s} variant="default" label={s} />
          ))}
        </div>
        <p style={{ fontSize: '0.75rem', color: 'var(--warn)', marginTop: 4 }}>
          Only the prefix is stored. Copy your full key when it is first created.
        </p>
      </div>
      <span style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', whiteSpace: 'nowrap' }}>
        {fmtDate(item.created_at)}
      </span>
      <button
        onClick={() => onRevoke(item.key_id)}
        style={{
          display: 'flex', alignItems: 'center', gap: 5,
          padding: '5px 10px', borderRadius: 'var(--r-sm)',
          fontSize: '0.8125rem', fontWeight: 500,
          color: 'var(--negative)', background: 'var(--negative-wash)',
          border: '1px solid var(--negative-line)', cursor: 'pointer',
          transition: 'all var(--duration-sm) var(--ease)',
        }}
      >
        <Trash2 size={12} />
        Revoke
      </button>
    </div>
  )
}

export default function SettingsPage() {
  const { apiKey } = useMarket()
  const { user, disconnect } = useAuth()
  const [keys, setKeys] = useState([])
  const [keyName, setKeyName] = useState('')
  const [creating, setCreating] = useState(false)
  const [newKey, setNewKey] = useState(null)
  const [toast, setToast] = useState(null)

  const showToast = (msg, type = 'info') => {
    setToast({ msg, type })
    setTimeout(() => setToast(null), 3000)
  }

  const refreshKeys = async () => {
    try {
      const result = await fetchAuthKeys(apiKey)
      setKeys(Array.isArray(result?.keys) ? result.keys : [])
    } catch (err) {
      showToast(err?.message ?? 'Failed to load API keys.', 'error')
    }
  }

  useEffect(() => { refreshKeys() }, [apiKey]) // eslint-disable-line

  const handleCreateKey = async (e) => {
    e.preventDefault()
    if (!keyName.trim()) return
    setCreating(true)
    setNewKey(null)
    try {
      const created = await createAuthKey(apiKey, keyName.trim(), ['caller', 'worker'])
      setNewKey(created.raw_key ?? null)
      showToast(`Key "${keyName}" created.`, 'success')
      setKeyName('')
      await refreshKeys()
    } catch (err) {
      showToast(err?.message ?? 'Failed to create key.', 'error')
    } finally {
      setCreating(false)
    }
  }

  const handleRevoke = async (keyId) => {
    try {
      await deleteAuthKey(apiKey, keyId)
      showToast('Key revoked.', 'success')
      await refreshKeys()
    } catch (err) {
      showToast(err?.message ?? 'Failed to revoke key.', 'error')
    }
  }

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ label: 'Settings' }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Toast */}
        {toast && (
          <div style={{
            position: 'fixed', top: 'var(--sp-5)', left: '50%', transform: 'translateX(-50%)',
            zIndex: 9000, padding: 'var(--sp-3) var(--sp-5)',
            background: toast.type === 'error' ? 'var(--negative-wash)' : 'var(--positive-wash)',
            color: toast.type === 'error' ? 'var(--negative)' : 'var(--positive)',
            border: '1px solid', borderColor: toast.type === 'error' ? 'var(--negative-line)' : 'var(--positive-line)',
            borderRadius: 'var(--r-pill)', fontSize: '0.875rem', fontWeight: 500,
            boxShadow: 'var(--shadow-lg)',
          }}>
            {toast.msg}
          </div>
        )}

        <div style={{ display: 'grid', gap: 'var(--sp-5)', maxWidth: 720 }}>

          {/* Account */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>Account</span>
            </Card.Header>
            <Card.Body>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-4)' }}>
                <Avatar name={user?.username ?? '?'} size="lg" />
                <div>
                  <p style={{ fontWeight: 600, fontSize: '1rem', color: 'var(--ink)', marginBottom: 2 }}>
                    {user?.username ?? '—'}
                  </p>
                  <p style={{ fontSize: '0.875rem', color: 'var(--ink-mute)' }}>
                    {user?.email ?? '—'}
                  </p>
                  {(user?.scopes ?? []).length > 0 && (
                    <div style={{ display: 'flex', gap: 'var(--sp-1)', marginTop: 'var(--sp-2)' }}>
                      {user.scopes.map(s => <Badge key={s} label={s} />)}
                    </div>
                  )}
                </div>
              </div>
            </Card.Body>
            <Card.Footer>
              <Button variant="ghost" size="sm" onClick={disconnect}>
                Sign out
              </Button>
            </Card.Footer>
          </Card>

          {/* New key revealed */}
          {newKey && (
            <div style={{
              padding: 'var(--sp-5)',
              background: 'var(--positive-wash)',
              border: '1px solid var(--positive-line)',
              borderRadius: 'var(--r-md)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sp-2)', marginBottom: 'var(--sp-3)' }}>
                <Key size={14} color="var(--positive)" />
                <p style={{ fontSize: '0.875rem', fontWeight: 600, color: 'var(--positive)' }}>
                  New API key — copy now, it won't be shown again
                </p>
              </div>
              <div style={{
                display: 'flex', alignItems: 'center', gap: 'var(--sp-3)',
                padding: 'var(--sp-3) var(--sp-4)',
                background: 'var(--surface)', border: '1px solid var(--positive-line)',
                borderRadius: 'var(--r-sm)',
              }}>
                <code style={{ flex: 1, fontFamily: 'var(--font-mono)', fontSize: '0.8125rem', color: 'var(--ink)', wordBreak: 'break-all' }}>
                  {newKey}
                </code>
                <button
                  onClick={() => { navigator.clipboard.writeText(newKey).catch(() => {}); showToast('Copied.', 'success') }}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 5,
                    padding: '5px 10px', borderRadius: 'var(--r-sm)',
                    fontSize: '0.8125rem', fontWeight: 500, color: 'var(--positive)',
                    background: 'var(--positive-wash)', border: '1px solid var(--positive-line)',
                    cursor: 'pointer', flexShrink: 0,
                  }}
                >
                  <Copy size={12} />
                  Copy
                </button>
              </div>
            </div>
          )}

          {/* Create key */}
          <Card>
            <Card.Header>
              <span style={{ fontWeight: 600, fontSize: '0.9375rem' }}>API keys</span>
            </Card.Header>
            <Card.Body>
              <form onSubmit={handleCreateKey} style={{ display: 'flex', gap: 'var(--sp-3)', alignItems: 'flex-end', marginBottom: 'var(--sp-5)' }}>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Input
                    label="Key name"
                    value={keyName}
                    onChange={e => setKeyName(e.target.value)}
                    placeholder="Production key"
                    required
                  />
                </div>
                <Button type="submit" variant="primary" size="md" loading={creating} icon={<Plus size={14} />}>
                  Create key
                </Button>
              </form>

              {keys.length === 0 ? (
                <EmptyState title="No API keys" sub="Create a key to authenticate API calls." />
              ) : (
                <div>
                  {/* Header */}
                  <div style={{
                    display: 'grid', gridTemplateColumns: '1fr auto auto',
                    gap: 'var(--sp-4)', padding: '8px 0',
                    borderBottom: '2px solid var(--line)',
                  }}>
                    {['Name / Prefix', 'Created', ''].map((h, i) => (
                      <span key={i} style={{
                        fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.05em',
                        textTransform: 'uppercase', color: 'var(--ink-mute)',
                      }}>
                        {h}
                      </span>
                    ))}
                  </div>
                  {keys.filter(k => k.is_active !== 0).map(item => (
                    <ApiKeyRow key={item.key_id} item={item} onRevoke={handleRevoke} />
                  ))}
                </div>
              )}
            </Card.Body>
          </Card>

        </div>
      </div>
    </main>
  )
}
