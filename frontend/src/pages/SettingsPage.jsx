import { useEffect, useState, useCallback } from 'react'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Avatar from '../ui/Avatar'
import Badge from '../ui/Badge'
import Reveal from '../ui/motion/Reveal'
import { useAuth } from '../context/AuthContext'
import { useTheme } from '../context/ThemeContext'
import { useMarket } from '../context/MarketContext'
import {
  authUpdateProfile,
  authChangePassword,
  listBillingTopups,
  listBillingPaymentMethods,
  deleteBillingPaymentMethod,
  createBillingSetupSession,
} from '../api'
import { Trash2, CreditCard, ExternalLink } from 'lucide-react'
import './SettingsPage.css'
import { fmtDate, fmtUsd } from '../utils/format.js'

function AccountForm({ user, apiKey, refreshProfile, showToast }) {
  const [fullName, setFullName] = useState(user?.full_name ?? '')
  const [username, setUsername] = useState(user?.username ?? '')
  const [email, setEmail] = useState(user?.email ?? '')
  const [phone, setPhone] = useState(user?.phone ?? '')
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    setFullName(user?.full_name ?? '')
    setUsername(user?.username ?? '')
    setEmail(user?.email ?? '')
    setPhone(user?.phone ?? '')
  }, [user?.full_name, user?.username, user?.email, user?.phone])

  const dirty = (
    (fullName ?? '') !== (user?.full_name ?? '')
    || (username ?? '') !== (user?.username ?? '')
    || (email ?? '') !== (user?.email ?? '')
    || (phone ?? '') !== (user?.phone ?? '')
  )

  const onSave = async (e) => {
    e.preventDefault()
    if (!apiKey || saving) return
    setError(null)
    setSaving(true)
    const fields = {}
    if ((fullName ?? '') !== (user?.full_name ?? '')) fields.full_name = fullName
    if ((username ?? '') !== (user?.username ?? '')) fields.username = username
    if ((email ?? '') !== (user?.email ?? '')) fields.email = email
    if ((phone ?? '') !== (user?.phone ?? '')) fields.phone = phone
    try {
      await authUpdateProfile(apiKey, fields)
      await refreshProfile?.()
      showToast?.('Profile updated', 'success')
    } catch (err) {
      setError(err?.message || 'Could not update profile.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="settings__form" onSubmit={onSave}>
      <div className="settings__form-grid">
        <label className="settings__field">
          <span className="settings__field-label">Full name</span>
          <Input value={fullName} onChange={e => setFullName(e.target.value)} placeholder="Ada Lovelace" />
        </label>
        <label className="settings__field">
          <span className="settings__field-label">Username</span>
          <Input value={username} onChange={e => setUsername(e.target.value)} placeholder="ada" />
        </label>
        <label className="settings__field">
          <span className="settings__field-label">Email</span>
          <Input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="ada@example.com" />
        </label>
        <label className="settings__field">
          <span className="settings__field-label">Phone</span>
          <Input value={phone} onChange={e => setPhone(e.target.value)} placeholder="+1 555 0100" />
        </label>
      </div>
      {error && <p className="settings__form-error" role="alert">{error}</p>}
      <div className="settings__form-actions">
        <Button type="submit" disabled={!dirty || saving} loading={saving}>Save changes</Button>
      </div>
    </form>
  )
}

function PasswordForm({ apiKey, showToast }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState(null)
  const [saving, setSaving] = useState(false)

  const onSave = async (e) => {
    e.preventDefault()
    if (!apiKey || saving) return
    setError(null)
    if (next !== confirm) {
      setError('New passwords do not match.')
      return
    }
    setSaving(true)
    try {
      await authChangePassword(apiKey, current, next)
      setCurrent(''); setNext(''); setConfirm('')
      showToast?.('Password updated. Sign in again on other devices.', 'success')
    } catch (err) {
      setError(err?.message || 'Could not change password.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="settings__form" onSubmit={onSave}>
      <div className="settings__form-grid">
        <label className="settings__field settings__field--full">
          <span className="settings__field-label">Current password</span>
          <Input type="password" value={current} onChange={e => setCurrent(e.target.value)} autoComplete="current-password" />
        </label>
        <label className="settings__field">
          <span className="settings__field-label">New password</span>
          <Input type="password" value={next} onChange={e => setNext(e.target.value)} autoComplete="new-password" />
        </label>
        <label className="settings__field">
          <span className="settings__field-label">Confirm new password</span>
          <Input type="password" value={confirm} onChange={e => setConfirm(e.target.value)} autoComplete="new-password" />
        </label>
      </div>
      {error && <p className="settings__form-error" role="alert">{error}</p>}
      <div className="settings__form-actions">
        <Button type="submit" disabled={!current || !next || !confirm || saving} loading={saving}>Change password</Button>
      </div>
    </form>
  )
}

function PaymentMethods({ apiKey, showToast }) {
  const [cards, setCards] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [adding, setAdding] = useState(false)
  const [removingId, setRemovingId] = useState(null)

  const load = useCallback(async () => {
    if (!apiKey) return
    setLoading(true)
    setError(null)
    try {
      const res = await listBillingPaymentMethods(apiKey)
      setCards(res?.payment_methods ?? [])
    } catch (err) {
      setError(err?.message || 'Could not load payment methods.')
    } finally {
      setLoading(false)
    }
  }, [apiKey])

  useEffect(() => { load() }, [load])

  const onAdd = async () => {
    if (!apiKey || adding) return
    setError(null)
    setAdding(true)
    try {
      const res = await createBillingSetupSession(apiKey)
      if (res?.checkout_url) {
        window.location.href = res.checkout_url
      } else {
        setError('Stripe did not return a setup URL.')
        setAdding(false)
      }
    } catch (err) {
      setError(err?.message || 'Could not start card setup.')
      setAdding(false)
    }
  }

  const onRemove = async (id) => {
    if (!apiKey || removingId) return
    setRemovingId(id)
    setError(null)
    try {
      await deleteBillingPaymentMethod(apiKey, id)
      setCards(cards.filter(c => c.id !== id))
      showToast?.('Card removed', 'success')
    } catch (err) {
      setError(err?.message || 'Could not remove card.')
    } finally {
      setRemovingId(null)
    }
  }

  return (
    <div className="settings__billing-block">
      <div className="settings__billing-head">
        <p className="settings__billing-title">Saved payment methods</p>
        <Button variant="secondary" size="sm" onClick={onAdd} disabled={adding} loading={adding}>
          Add payment method
        </Button>
      </div>
      {error && <p className="settings__form-error" role="alert">{error}</p>}
      {loading ? (
        <p className="settings__billing-empty">Loading…</p>
      ) : cards.length === 0 ? (
        <p className="settings__billing-empty">No saved cards yet.</p>
      ) : (
        <ul className="settings__cards">
          {cards.map(card => (
            <li key={card.id} className="settings__card-row">
              <div className="settings__card-icon"><CreditCard size={16} /></div>
              <div className="settings__card-info">
                <p className="settings__card-brand">{(card.brand || 'card').toUpperCase()} •••• {card.last4 || '????'}</p>
                <p className="settings__card-exp">Expires {String(card.exp_month || '?').padStart(2, '0')}/{card.exp_year || '?'}</p>
              </div>
              <button
                type="button"
                className="settings__revoke-btn"
                onClick={() => onRemove(card.id)}
                disabled={removingId === card.id}
                aria-label={`Remove card ending ${card.last4 || ''}`}
              >
                <Trash2 size={12} />
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function TopupHistory({ apiKey }) {
  const [topups, setTopups] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!apiKey) return
    let cancelled = false
    ;(async () => {
      try {
        const res = await listBillingTopups(apiKey, 25)
        if (!cancelled) setTopups(res?.topups ?? [])
      } catch (err) {
        if (!cancelled) setError(err?.message || 'Could not load top-up history.')
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => { cancelled = true }
  }, [apiKey])

  return (
    <div className="settings__billing-block">
      <p className="settings__billing-title">Top-up history</p>
      {error && <p className="settings__form-error" role="alert">{error}</p>}
      {loading ? (
        <p className="settings__billing-empty">Loading…</p>
      ) : topups.length === 0 ? (
        <p className="settings__billing-empty">No top-ups yet. Visit Wallet to add funds.</p>
      ) : (
        <table className="settings__topups">
          <thead>
            <tr>
              <th>Date</th>
              <th>Amount</th>
              <th>Session</th>
            </tr>
          </thead>
          <tbody>
            {topups.map(t => (
              <tr key={t.session_id}>
                <td>{fmtDate(t.processed_at)}</td>
                <td>{fmtUsd(t.amount_cents)}</td>
                <td className="t-mono settings__topup-session">{t.session_id.slice(0, 16)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

export default function SettingsPage() {
  const { user, disconnect, refreshProfile, apiKey } = useAuth()
  const { showToast } = useMarket()

  let theme = null, setTheme = null
  try { const t = useTheme?.(); theme = t?.theme ?? null; setTheme = t?.setTheme ?? null } catch {}

  return (
    <main className="settings">
      <Topbar crumbs={[{ label: 'Settings' }]} />

      <div className="settings__scroll">
        <div className="settings__content">

          {/* Account header */}
          <Reveal>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Account</span>
              </Card.Header>
              <Card.Body>
                <div className="settings__account-row">
                  <Avatar name={user?.username ?? '?'} size="lg" />
                  <div className="settings__account-info">
                    <p className="settings__username">{user?.full_name || user?.username || '-'}</p>
                    <p className="settings__email">{user?.email ?? '-'}</p>
                    {(user?.scopes ?? []).length > 0 && (
                      <div className="settings__scopes">
                        {user.scopes.map(s => <Badge key={s} label={s} />)}
                      </div>
                    )}
                  </div>
                </div>
                <hr className="settings__divider" />
                <AccountForm
                  user={user}
                  apiKey={apiKey}
                  refreshProfile={refreshProfile}
                  showToast={showToast}
                />
              </Card.Body>
              <Card.Footer>
                <Button variant="ghost" size="sm" onClick={disconnect}>Sign out</Button>
              </Card.Footer>
            </Card>
          </Reveal>

          {/* Change password */}
          <Reveal delay={0.06}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Change password</span>
              </Card.Header>
              <Card.Body>
                <PasswordForm apiKey={apiKey} showToast={showToast} />
              </Card.Body>
            </Card>
          </Reveal>

          {/* Billing */}
          <Reveal delay={0.1}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Billing</span>
              </Card.Header>
              <Card.Body>
                <PaymentMethods apiKey={apiKey} showToast={showToast} />
                <hr className="settings__divider" />
                <TopupHistory apiKey={apiKey} />
                <p className="settings__billing-note">
                  Wallet top-ups happen on the Wallet page.
                  <a href="/wallet" className="settings__billing-link">
                    Go to wallet <ExternalLink size={11} />
                  </a>
                </p>
              </Card.Body>
            </Card>
          </Reveal>

          {/* Appearance */}
          {setTheme && (
            <Reveal delay={0.14}>
              <Card>
                <Card.Header>
                  <span className="settings__section-title">Appearance</span>
                </Card.Header>
                <Card.Body>
                  <div className="settings__theme-row">
                    {['dark', 'light', 'system'].map(mode => (
                      <button
                        key={mode}
                        type="button"
                        className={`settings__theme-chip ${theme === mode ? 'is-active' : ''}`}
                        onClick={() => setTheme(mode)}
                      >
                        {mode[0].toUpperCase() + mode.slice(1)}
                      </button>
                    ))}
                  </div>
                </Card.Body>
              </Card>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
