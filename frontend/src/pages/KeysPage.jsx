import { useEffect, useState } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Skeleton from '../ui/Skeleton'
import Reveal from '../ui/motion/Reveal'
import Pill from '../ui/Pill'
import { createAuthKey, deleteAuthKey, fetchAuthKeys } from '../api'
import { useMarket } from '../context/MarketContext'
import { Key, Plus, Trash2, Copy, AlertTriangle } from 'lucide-react'
import './SettingsPage.css'

function fmtDate(str) {
  if (!str) return 'Never'
  return new Date(str).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

const SCOPE_OPTIONS = [
  { value: 'caller', label: 'Caller', desc: 'hire agents, create jobs' },
  { value: 'worker', label: 'Worker', desc: 'claim and complete jobs' },
]

function ApiKeyRow({ item, onRevoke, revoking }) {
  const [confirming, setConfirming] = useState(false)
  if (confirming) {
    return (
      <div className="settings__key-row settings__key-row--confirming">
        <div className="settings__revoke-confirm">
          <AlertTriangle size={13} color="var(--negative, #ef4444)" />
          <p className="settings__revoke-confirm-msg">
            Revoke <strong>{item.name}</strong>? Any code using this key will immediately stop working.
          </p>
        </div>
        <div className="settings__revoke-confirm-actions">
          <Button size="sm" variant="danger" loading={revoking} onClick={() => onRevoke(item.key_id)}>Revoke</Button>
          <Button size="sm" variant="ghost" disabled={revoking} onClick={() => setConfirming(false)}>Cancel</Button>
        </div>
      </div>
    )
  }
  return (
    <div className="settings__key-row">
      <div>
        <p className="settings__key-name">{item.name}</p>
        <div className="settings__key-meta">
          <span className="settings__key-prefix t-mono">{item.key_prefix}…</span>
          <span className="settings__key-last-used">Last used: {fmtDate(item.last_used_at)}</span>
          {(item.scopes ?? []).map(s => <Badge key={s} label={s} />)}
        </div>
      </div>
      <span className="settings__key-created">{fmtDate(item.created_at)}</span>
      <button onClick={() => setConfirming(true)} className="settings__revoke-btn" aria-label={`Revoke key ${item.name}`}>
        <Trash2 size={12} />
        Revoke
      </button>
    </div>
  )
}

export default function KeysPage() {
  const { apiKey, showToast } = useMarket()
  const [keys, setKeys] = useState([])
  const [keysLoading, setKeysLoading] = useState(true)
  const [keysError, setKeysError] = useState(null)
  const [keyName, setKeyName] = useState('')
  const [keyScopes, setKeyScopes] = useState(['caller', 'worker'])
  const [perJobCapDollars, setPerJobCapDollars] = useState('1.00')
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState(null)
  const [newKey, setNewKey] = useState(null)
  const [revoking, setRevoking] = useState(null)

  const refreshKeys = async () => {
    setKeysError(null)
    try {
      const result = await fetchAuthKeys(apiKey)
      setKeys(Array.isArray(result?.keys) ? result.keys : [])
    } catch (err) {
      setKeysError(err?.message ?? 'Failed to load API keys.')
    } finally {
      setKeysLoading(false)
    }
  }

  useEffect(() => { refreshKeys() }, [apiKey]) // eslint-disable-line

  const handleCreateKey = async (e) => {
    e.preventDefault()
    const name = keyName.trim()
    if (!name) return
    if (keyScopes.length === 0) { setCreateError('Select at least one scope.'); return }
    const options = {}
    if (keyScopes.includes('caller')) {
      const dollars = Number(perJobCapDollars)
      if (!Number.isFinite(dollars) || dollars <= 0) {
        setCreateError('Enter a per-job spending cap (in USD) for caller-scoped keys.')
        return
      }
      options.per_job_cap_cents = Math.round(dollars * 100)
    }
    setCreating(true); setCreateError(null); setNewKey(null)
    try {
      const created = await createAuthKey(apiKey, name, keyScopes, options)
      setNewKey(created.raw_key ?? null)
      showToast?.(`Key "${name}" created.`, 'success')
      setKeyName('')
      await refreshKeys()
    } catch (err) {
      setCreateError(err?.message ?? 'Failed to create key.')
    } finally {
      setCreating(false)
    }
  }

  const handleRevoke = async (keyId) => {
    setRevoking(keyId)
    try {
      await deleteAuthKey(apiKey, keyId)
      showToast?.('Key revoked.', 'success')
      await refreshKeys()
    } catch (err) {
      showToast?.(err?.message ?? 'Failed to revoke key.', 'error')
    } finally {
      setRevoking(null)
    }
  }

  return (
    <main className="settings">
      <Topbar crumbs={[{ label: 'API Keys' }]} />
      <div className="settings__scroll">
        <div className="settings__content">
          <Reveal>
            <div className="settings__intro">
              <h1 className="settings__page-title">API keys</h1>
              <p className="settings__page-sub">
                Create scoped keys for each integration. Caller keys hire agents; worker keys claim jobs.
                Rotate a key by revoking the old one and creating a new one — keys are shown only once at creation.
              </p>
            </div>
          </Reveal>

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
                    onClick={() => { navigator.clipboard.writeText(newKey).catch(() => {}); showToast?.('Copied.', 'success') }}
                    className="settings__copy-btn settings__copy-btn--positive"
                  >
                    <Copy size={12} />
                    Copy
                  </button>
                </div>
              </div>
            </Reveal>
          )}

          <Reveal delay={0.05}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Create a new key</span>
              </Card.Header>
              <Card.Body>
                <form onSubmit={handleCreateKey} className="settings__key-create">
                  <div className="settings__input-wrap">
                    <Input
                      label="Key name"
                      value={keyName}
                      onChange={e => { setKeyName(e.target.value); setCreateError(null) }}
                      placeholder="Production key"
                      required
                    />
                  </div>
                  {keyScopes.includes('caller') && (
                    <div className="settings__input-wrap">
                      <Input
                        label="Per-job spending cap (USD)"
                        type="number"
                        min="0"
                        step="0.01"
                        value={perJobCapDollars}
                        onChange={e => { setPerJobCapDollars(e.target.value); setCreateError(null) }}
                        placeholder="1.00"
                        hint="Required for caller scope. Each job using this key cannot exceed this price."
                      />
                    </div>
                  )}
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
                            onClick={() => {
                              setCreateError(null)
                              setKeyScopes(prev => active ? prev.filter(s => s !== opt.value) : [...prev, opt.value])
                            }}
                          >
                            {opt.label}
                          </Pill>
                        )
                      })}
                    </div>
                  </div>
                  {createError && <p className="settings__create-error">{createError}</p>}
                  <Button type="submit" variant="primary" size="md" loading={creating} disabled={!keyName.trim()} icon={<Plus size={14} />}>
                    Create key
                  </Button>
                </form>
              </Card.Body>
            </Card>
          </Reveal>

          <Reveal delay={0.1}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Your keys</span>
              </Card.Header>
              <Card.Body>
                {keysLoading ? (
                  <div className="settings__keys-loading">
                    {[1, 2].map(i => <Skeleton key={i} variant="rect" height={72} />)}
                  </div>
                ) : keysError ? (
                  <div className="settings__keys-error">
                    <p>{keysError}</p>
                    <Button variant="ghost" size="sm" onClick={refreshKeys}>Retry</Button>
                  </div>
                ) : keys.length === 0 ? (
                  <EmptyState title="No API keys yet" sub="Create your first key above to authenticate API calls." />
                ) : (
                  <div>
                    <div className="settings__keys-head">
                      <span>Name / Prefix</span>
                      <span>Created</span>
                      <span aria-hidden="true" />
                    </div>
                    {keys.filter(k => k.is_active !== 0).map(item => (
                      <ApiKeyRow key={item.key_id} item={item} onRevoke={handleRevoke} revoking={revoking === item.key_id} />
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
