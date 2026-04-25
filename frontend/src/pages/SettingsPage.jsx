import { useState } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Avatar from '../ui/Avatar'
import Badge from '../ui/Badge'
import Reveal from '../ui/motion/Reveal'
import { useAuth } from '../context/AuthContext'
import { useTheme } from '../context/ThemeContext'
import { authUpdateRole } from '../api'
import { Key, ArrowRight } from 'lucide-react'
import './SettingsPage.css'

const ROLE_OPTIONS = [
  { value: 'hirer',   label: 'Hirer',   sub: 'Hire agents and manage jobs' },
  { value: 'builder', label: 'Builder',  sub: 'List skills and earn revenue' },
  { value: 'both',    label: 'Both',     sub: 'Full access to all features' },
]

export default function SettingsPage() {
  const { user, disconnect, refreshProfile } = useAuth()
  const [roleSaving, setRoleSaving] = useState(false)
  const [roleError, setRoleError] = useState('')
  const currentRole = user?.role ?? 'both'

  const handleRoleSwitch = async (role) => {
    if (role === currentRole || roleSaving) return
    setRoleSaving(true)
    setRoleError('')
    try {
      await authUpdateRole(role)
      await refreshProfile()
    } catch (e) {
      setRoleError(e?.message ?? 'Failed to update role.')
    } finally {
      setRoleSaving(false)
    }
  }

  // Theme hook is optional — guard it so it doesn't crash if context missing.
  let theme = null, setTheme = null
  try { const t = useTheme?.(); theme = t?.theme ?? null; setTheme = t?.setTheme ?? null } catch {}

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
                    <p className="settings__username">{user?.username ?? '-'}</p>
                    <p className="settings__email">{user?.email ?? '-'}</p>
                    {(user?.scopes ?? []).length > 0 && (
                      <div className="settings__scopes">
                        {user.scopes.map(s => <Badge key={s} label={s} />)}
                      </div>
                    )}
                  </div>
                </div>
              </Card.Body>
              <Card.Footer>
                <Button variant="ghost" size="sm" onClick={disconnect}>Sign out</Button>
              </Card.Footer>
            </Card>
          </Reveal>

          {/* Role */}
          <Reveal delay={0.05}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">Role</span>
              </Card.Header>
              <Card.Body>
                <p style={{ fontSize: '0.8125rem', color: 'var(--text-muted)', marginBottom: 12, lineHeight: 1.55 }}>
                  Your role controls which features appear in the sidebar. You can switch at any time.
                </p>
                <div className="settings__theme-row">
                  {ROLE_OPTIONS.map(({ value, label }) => (
                    <button
                      key={value}
                      type="button"
                      className={`settings__theme-chip ${currentRole === value ? 'is-active' : ''}`}
                      onClick={() => handleRoleSwitch(value)}
                      disabled={roleSaving}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {roleError && (
                  <p style={{ marginTop: 8, fontSize: '0.8125rem', color: 'var(--negative)' }}>{roleError}</p>
                )}
              </Card.Body>
            </Card>
          </Reveal>

          {/* API keys pointer */}
          <Reveal delay={0.08}>
            <Card>
              <Card.Header>
                <span className="settings__section-title">API keys</span>
              </Card.Header>
              <Card.Body>
                <div className="settings__keys-pointer">
                  <div className="settings__keys-pointer-icon">
                    <Key size={18} />
                  </div>
                  <div className="settings__keys-pointer-copy">
                    <p className="settings__keys-pointer-title">Manage keys on their own page</p>
                    <p className="settings__keys-pointer-sub">
                      Create, list, and revoke scoped API keys from the new API Keys tab.
                    </p>
                  </div>
                  <Link to="/keys" className="settings__keys-pointer-link">
                    Open API Keys
                    <ArrowRight size={13} />
                  </Link>
                </div>
              </Card.Body>
            </Card>
          </Reveal>

          {/* Appearance */}
          {setTheme && (
            <Reveal delay={0.08}>
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
