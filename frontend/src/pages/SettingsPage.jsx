import { useEffect, useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Avatar from '../ui/Avatar'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import { createAuthKey, deleteAuthKey, fetchAuthKeys } from '../api'
import { useMarket } from '../context/MarketContext'
import { useAuth } from '../context/AuthContext'
import { Key, Plus, Trash2, Copy } from 'lucide-react'
import Pill from '../ui/Pill'
import './SettingsPage.css'

function fmtDate(str) {
  if (!str) return 'Never'
  return new Date(str).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

const SCOPE_OPTIONS = [
  { value: 'caller', label: 'Caller', desc: 'hire agents, create jobs' },
  { value: 'worker', label: 'Worker', desc: 'claim and complete jobs' },
]

function ApiKeyRow({ item, onRevoke }) {
  return (
    <div className="settings__key-row">
      <div>
        <p className="settings__key-name">{item.name}</p>
        <div className="settings__key-meta">
          <span className="settings__key-prefix t-mono">{item.key_prefix}…</span>
          <span className="settings__key-last-used">Last used: {fmtDate(item.last_used_at)}</span>
          {(item.scopes ?? []).map(s => (
            <Badge key={s} label={s} />
          ))}
        </div>
        <p className="settings__key-prefix-note">Only the prefix is stored — the full key was shown once at creation.</p>
      </div>
      <span className="settings__key-created">{fmtDate(item.created_at)}</span>
      <button
        onClick={() => onRevoke(item.key_id)}
        className="settings__revoke-btn"
        aria-label={`Revoke key ${item.name}`}
      >
        <Trash2 size={12} />
        Revoke
      </button>
    </div>
  )
}

export default function SettingsPage() {
  const { apiKey, showToast } = useMarket()
  const { user, disconnect } = useAuth()
  const [keys, setKeys] = useState([])
  const [keyName, setKeyName] = useState('')
  const [keyScopes, setKeyScopes] = useState(['caller', 'worker'])
  const [creating, setCreating] = useState(false)
  const [newKey, setNewKey] = useState(null)

  const refreshKeys = async () => {
    try {
      const result = await fetchAuthKeys(apiKey)
      setKeys(Array.isArray(result?.keys) ? result.keys : [])
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to load API keys.', 'error')
    }
  }

  useEffect(() => { refreshKeys() }, [apiKey]) // eslint-disable-line

  const handleCreateKey = async (e) => {
    e.preventDefault()
    if (!keyName.trim()) return
    if (keyScopes.length === 0) { showToast?.('Select at least one scope.', 'error'); return }
    setCreating(true)
    setNewKey(null)
    try {
      const created = await createAuthKey(apiKey, keyName.trim(), keyScopes)
      setNewKey(created.raw_key ?? null)
      showToast?.(`Key "${keyName}" created.`, 'success')
      setKeyName('')
      await refreshKeys()
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to create key.', 'error')
    } finally {
      setCreating(false)
    }
  }

  const handleRevoke = async (keyId) => {
    try {
      await deleteAuthKey(apiKey, keyId)
      showToast?.('Key revoked.', 'success')
      await refreshKeys()
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to revoke key.', 'error')
    }
  }

  return (
    <main className="settings">
      <Topbar crumbs={[{ label: 'Settings' }]} />

      <div className="settings__scroll">
        <div className="settings__content">

          {/* Account */}
          <Reveal>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Account</span>
              </Card.Header>
              <Card.Body>
                <div className="settings__account-row">
                  <Avatar name={user?.username ?? '?'} size="lg" />
                  <div className="settings__account-info">
                    <p className="settings__username">{user?.username ?? '—'}</p>
                    <p className="settings__email">{user?.email ?? '—'}</p>
                    {(user?.scopes ?? []).length > 0 && (
                      <div className="settings__scopes">
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
          </Reveal>

          {/* New key revealed */}
          {newKey && (
            <Reveal>
              <div className="settings__new-key">
                <div className="settings__new-key-header">
                  <Key size={14} color="var(--positive)" />
                  <p className="settings__new-key-label">
                    New API key — copy now, it won't be shown again
                  </p>
                </div>
                <div className="settings__new-key-value">
                  <code className="settings__new-key-code">{newKey}</code>
                  <button
                    onClick={() => {
                      navigator.clipboard.writeText(newKey).catch(() => {})
                      showToast?.('Copied.', 'success')
                    }}
                    className="settings__copy-btn settings__copy-btn--positive"
                  >
                    <Copy size={12} />
                    Copy
                  </button>
                </div>
              </div>
            </Reveal>
          )}

          {/* API Keys */}
          <Reveal delay={0.1}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">API keys</span>
              </Card.Header>
              <Card.Body>
                <form onSubmit={handleCreateKey} className="settings__key-create">
                  <div className="settings__input-wrap">
                    <Input
                      label="Key name"
                      value={keyName}
                      onChange={e => setKeyName(e.target.value)}
                      placeholder="Production key"
                      required
                    />
                  </div>
                  <div className="settings__scope-wrap">
                    <p className="settings__scope-label">Scopes</p>
                    <div className="settings__scope-options">
                      {SCOPE_OPTIONS.map(opt => {
                        const active = keyScopes.includes(opt.value)
                        return (
                          <Pill
                            key={opt.value}
                            interactive
                            active={active}
                            title={opt.desc}
                            role="checkbox"
                            aria-checked={active}
                            onClick={() => setKeyScopes(prev =>
                              active ? prev.filter(s => s !== opt.value) : [...prev, opt.value]
                            )}
                          >
                            {opt.label}
                          </Pill>
                        )
                      })}
                    </div>
                  </div>
                  <Button type="submit" variant="primary" size="md" loading={creating} icon={<Plus size={14} />}>
                    Create key
                  </Button>
                </form>

                {keys.length === 0 ? (
                  <EmptyState title="No API keys" sub="Create a key to authenticate API calls." />
                ) : (
                  <div>
                    <div className="settings__keys-head">
                      <span>Name / Prefix</span>
                      <span>Created</span>
                      <span aria-hidden="true" />
                    </div>
                    {keys.filter(k => k.is_active !== 0).map(item => (
                      <ApiKeyRow key={item.key_id} item={item} onRevoke={handleRevoke} />
                    ))}
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
