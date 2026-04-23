import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authLogin, authRegister } from '../../api'
import { useAuth } from '../../context/AuthContext'
import Button from '../../ui/Button'
import Input from '../../ui/Input'
import { Mail, Lock, User, Eye, EyeOff } from 'lucide-react'
import './AuthPanel.css'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export default function AuthPanel() {
  const { connect } = useAuth()
  const navigate = useNavigate()
  const [tab, setTab] = useState('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [username, setUsername] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const registerMode = tab === 'register'
  const normalizedEmail = email.trim().toLowerCase()
  const normalizedUsername = username.trim()
  const passwordChecks = {
    length: password.length >= 8,
    letter: /[A-Za-z]/.test(password),
    number: /\d/.test(password),
  }
  const emailValid = EMAIL_RE.test(normalizedEmail)
  const registerFormValid =
    normalizedUsername.length >= 3 &&
    normalizedUsername.length <= 32 &&
    /^[a-zA-Z0-9_-]+$/.test(normalizedUsername) &&
    emailValid &&
    passwordChecks.length &&
    passwordChecks.letter &&
    passwordChecks.number &&
    password === confirmPassword
  const signinFormValid = emailValid && password.length > 0
  const canSubmit = registerMode ? registerFormValid : signinFormValid

  const switchTab = (nextTab) => {
    setTab(nextTab)
    setError('')
    setPassword('')
    setConfirmPassword('')
    setShowPassword(false)
    setShowConfirmPassword(false)
  }

  // Allow external callers (landing nav) to focus a specific auth tab by
  // dispatching `aztea:auth-tab` with `{ tab: 'signin' | 'register' }`.
  useEffect(() => {
    const handler = (event) => {
      const next = event?.detail?.tab
      if (next === 'signin' || next === 'register') switchTab(next)
    }
    window.addEventListener('aztea:auth-tab', handler)
    return () => window.removeEventListener('aztea:auth-tab', handler)
  }, [])

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    if (!canSubmit) {
      if (!registerMode) {
        setError('Enter a valid email and password to sign in.')
      } else if (normalizedUsername.length < 3) {
        setError('Username must be at least 3 characters.')
      } else if (normalizedUsername.length > 32) {
        setError('Username must be 32 characters or fewer.')
      } else if (!/^[a-zA-Z0-9_-]+$/.test(normalizedUsername)) {
        setError('Username can only use letters, numbers, underscore, and hyphen.')
      } else if (!emailValid) {
        setError('Enter a valid email address.')
      } else if (!passwordChecks.length || !passwordChecks.letter || !passwordChecks.number) {
        setError('Password must be at least 8 characters and include letters and numbers.')
      } else if (password !== confirmPassword) {
        setError('Passwords do not match yet.')
      } else {
        setError('Please complete all required fields before creating your account.')
      }
      return
    }
    setLoading(true)
    try {
      let result
      if (!registerMode) {
        result = await authLogin(normalizedEmail, password)
      } else {
        result = await authRegister(normalizedUsername, normalizedEmail, password)
      }
      const userInfo = {
        user_id: result.user_id,
        username: result.username ?? normalizedUsername,
        email: result.email ?? normalizedEmail,
        scopes: result.scopes ?? ['caller'],
        legal_acceptance_required: Boolean(result.legal_acceptance_required),
        legal_accepted_at: result.legal_accepted_at ?? null,
        terms_version_current: result.terms_version_current ?? null,
        privacy_version_current: result.privacy_version_current ?? null,
        terms_version_accepted: result.terms_version_accepted ?? null,
        privacy_version_accepted: result.privacy_version_accepted ?? null,
      }
      if (registerMode && result.user_id) {
        localStorage.removeItem(`aztea_onboarding_done:${result.user_id}`)
      }
      connect(result.raw_api_key, userInfo)
      navigate('/')
    } catch (err) {
      setError(err.message ?? 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth-panel">
      <div className="auth-panel__tabs">
        <button
          className={`auth-panel__tab ${tab === 'signin' ? 'auth-panel__tab--active' : ''}`}
          onClick={() => switchTab('signin')}
          type="button"
        >
          Sign in
        </button>
        <button
          className={`auth-panel__tab ${tab === 'register' ? 'auth-panel__tab--active' : ''}`}
          onClick={() => switchTab('register')}
          type="button"
        >
          Create account
        </button>
      </div>
      <div className="auth-panel__body">
        <form className="auth-panel__form" onSubmit={handleSubmit}>
          {tab === 'register' && (
            <Input
              label="Username"
              type="text"
              placeholder="satoshi"
              value={username}
              onChange={e => setUsername(e.target.value)}
              maxLength={32}
              required
              autoComplete="username"
              iconLeft={<User size={14} />}
              hint="3-32 chars; letters, numbers, underscore, hyphen."
            />
          )}
          <Input
              label="Email"
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={e => setEmail(e.target.value)}
              onBlur={e => setEmail(e.target.value.trim().toLowerCase())}
              required
              autoComplete="email"
              iconLeft={<Mail size={14} />}
            />
            <Input
              label="Password"
              type={showPassword ? 'text' : 'password'}
              placeholder="••••••••"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
              autoComplete={tab === 'signin' ? 'current-password' : 'new-password'}
              iconLeft={<Lock size={14} />}
              iconRight={
                <button
                  type="button"
                  className="auth-panel__pw-toggle"
                  onClick={() => setShowPassword(v => !v)}
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                >
                  {showPassword ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              }
              hint={
                tab === 'register'
                  ? 'Use at least 8 characters with letters and numbers.'
                  : undefined
              }
            />
            {tab === 'register' && (
              <Input
                label="Confirm password"
                type={showConfirmPassword ? 'text' : 'password'}
                placeholder="••••••••"
                value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                required
                autoComplete="new-password"
                iconLeft={<Lock size={14} />}
                iconRight={
                  <button
                    type="button"
                    className="auth-panel__pw-toggle"
                    onClick={() => setShowConfirmPassword(v => !v)}
                    aria-label={showConfirmPassword ? 'Hide confirm password' : 'Show confirm password'}
                  >
                    {showConfirmPassword ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                }
              />
            )}
            {tab === 'register' && (
              <div className="auth-panel__checks">
                <span className={passwordChecks.length ? 'ok' : ''}>8+ chars</span>
                <span className={passwordChecks.letter ? 'ok' : ''}>letter</span>
                <span className={passwordChecks.number ? 'ok' : ''}>number</span>
                <span className={password === confirmPassword && confirmPassword ? 'ok' : ''}>passwords match</span>
              </div>
            )}
            {error && <p className="auth-panel__error">{error}</p>}
            <Button type="submit" variant="primary" size="md" loading={loading} disabled={!canSubmit} style={{ width: '100%' }}>
              {tab === 'signin' ? 'Sign in' : 'Create account'}
            </Button>
          <p className="auth-panel__hint">
            {tab === 'signin'
              ? 'New here? Switch to "Create account" above.'
              : 'Already have an account? Sign in above.'}
          </p>
        </form>
      </div>
    </div>
  )
}
