import { useEffect, useState } from 'react'
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom'
import { authLogin, authRegister, authForgotPassword, authResetPassword } from '../../api'
import { useAuth } from '../../context/AuthContext'
import Button from '../../ui/Button'
import Input from '../../ui/Input'
import { Mail, Lock, User, Eye, EyeOff, Copy, Check, KeyRound, ArrowLeft, Hammer, Zap } from 'lucide-react'
import './AuthPanel.css'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export default function AuthPanel() {
  const { connect } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const redirectTo = searchParams.get('redirect')
    ?? (location.state?.from && location.state.from !== '/welcome' ? location.state.from : '/')
  const [tab, setTab] = useState('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [username, setUsername] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [apiKeyReveal, setApiKeyReveal] = useState(null)
  const [copied, setCopied] = useState(false)
  const [keyAcknowledged, setKeyAcknowledged] = useState(false)

  // Role selector state (register flow only)
  const [registerStep, setRegisterStep] = useState('role') // 'role' | 'form'
  const [selectedRole, setSelectedRole] = useState(null) // 'builder' | 'hirer'

  // Forgot password state
  const [forgotStep, setForgotStep] = useState(1) // 1 = email, 2 = otp+newpw
  const [forgotEmail, setForgotEmail] = useState('')
  const [otp, setOtp] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [showNewPassword, setShowNewPassword] = useState(false)
  const [forgotSuccess, setForgotSuccess] = useState(false)

  const registerMode = tab === 'register'
  const forgotMode = tab === 'forgot'
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
    if (nextTab === 'register') {
      setRegisterStep('role')
      setSelectedRole(null)
    }
    if (nextTab === 'forgot') {
      setForgotStep(1)
      setForgotEmail(email) // pre-fill from signin email if present
      setOtp('')
      setNewPassword('')
      setForgotSuccess(false)
    }
  }

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
    if (loading) return // double-submit guard
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
        result = await authRegister(normalizedUsername, normalizedEmail, password, selectedRole || 'both')
      }
      const userInfo = {
        user_id: result.user_id,
        username: result.username ?? normalizedUsername,
        email: result.email ?? normalizedEmail,
        role: result.role ?? selectedRole ?? 'both',
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
      if (registerMode && result.raw_api_key) {
        setApiKeyReveal({ rawKey: result.raw_api_key, userInfo })
        return
      }
      connect(result.raw_api_key, userInfo)
      navigate(redirectTo)
    } catch (err) {
      setError(err.message ?? 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  const handleCopyKey = async () => {
    if (!apiKeyReveal?.rawKey) return
    try {
      await navigator.clipboard.writeText(apiKeyReveal.rawKey)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      setError('Unable to copy - select the key manually and copy it.')
    }
  }

  const handleContinueAfterReveal = () => {
    if (!apiKeyReveal) return
    connect(apiKeyReveal.rawKey, apiKeyReveal.userInfo)
    setApiKeyReveal(null)
    setKeyAcknowledged(false)
    navigate(redirectTo)
  }

  const handleForgotSendOtp = async (e) => {
    e.preventDefault()
    if (loading) return
    const normalized = forgotEmail.trim().toLowerCase()
    if (!EMAIL_RE.test(normalized)) {
      setError('Enter a valid email address.')
      return
    }
    setLoading(true)
    setError('')
    try {
      await authForgotPassword(normalized)
      setForgotEmail(normalized)
      setForgotStep(2)
    } catch (err) {
      setError(err.message ?? 'Failed to send reset code. Try again.')
    } finally {
      setLoading(false)
    }
  }

  const handleForgotReset = async (e) => {
    e.preventDefault()
    if (loading) return
    if (otp.trim().length !== 6) {
      setError('Enter the 6-digit code from your email.')
      return
    }
    if (newPassword.length < 8 || !/[A-Za-z]/.test(newPassword) || !/\d/.test(newPassword)) {
      setError('Password must be at least 8 characters and include letters and numbers.')
      return
    }
    setLoading(true)
    setError('')
    try {
      await authResetPassword(forgotEmail, otp.trim(), newPassword)
      setForgotSuccess(true)
    } catch (err) {
      setError(err.message ?? 'Reset failed. Check your code and try again.')
    } finally {
      setLoading(false)
    }
  }

  if (apiKeyReveal) {
    return (
      <div className="auth-panel">
        <div className="auth-panel__body">
          <div className="auth-panel__reveal">
            <div className="auth-panel__reveal-icon" aria-hidden>
              <KeyRound size={18} />
            </div>
            <h3 className="auth-panel__reveal-title">Save your API key</h3>
            <p className="auth-panel__reveal-sub">
              This is the only time we show your full key. Store it somewhere safe - a password manager
              or a local .env file. You can mint scoped keys later in Settings → API Keys.
            </p>
            <div className="auth-panel__reveal-keybox">
              <code className="auth-panel__reveal-key">{apiKeyReveal.rawKey}</code>
              <button
                type="button"
                className="auth-panel__reveal-copy"
                onClick={handleCopyKey}
                aria-label="Copy API key"
              >
                {copied ? <Check size={14} /> : <Copy size={14} />}
                <span>{copied ? 'Copied' : 'Copy'}</span>
              </button>
            </div>
            <label className="auth-panel__reveal-ack">
              <input
                type="checkbox"
                checked={keyAcknowledged}
                onChange={e => setKeyAcknowledged(e.target.checked)}
              />
              <span>I've saved this key somewhere safe.</span>
            </label>
            {error && <p className="auth-panel__error">{error}</p>}
            <Button
              type="button"
              variant="primary"
              size="md"
              onClick={handleContinueAfterReveal}
              disabled={!keyAcknowledged}
              style={{ width: '100%' }}
            >
              Continue
            </Button>
          </div>
        </div>
      </div>
    )
  }

  if (forgotMode) {
    return (
      <div className="auth-panel">
        <div className="auth-panel__tabs">
          <button
            className="auth-panel__tab-back"
            type="button"
            onClick={() => switchTab('signin')}
          >
            <ArrowLeft size={13} />
            <span>Back to sign in</span>
          </button>
        </div>
        <div className="auth-panel__body">
          {forgotSuccess ? (
            <div className="auth-panel__forgot-success">
              <p className="auth-panel__forgot-success-msg">
                Password reset. You can now sign in with your new password.
              </p>
              <Button
                type="button"
                variant="primary"
                size="md"
                onClick={() => switchTab('signin')}
                style={{ width: '100%' }}
              >
                Sign in
              </Button>
            </div>
          ) : forgotStep === 1 ? (
            <form className="auth-panel__form" onSubmit={handleForgotSendOtp}>
              <p className="auth-panel__forgot-desc">
                Enter your account email and we'll send a one-time code to reset your password.
              </p>
              <Input
                label="Email"
                type="email"
                placeholder="you@example.com"
                value={forgotEmail}
                onChange={e => setForgotEmail(e.target.value)}
                onBlur={e => setForgotEmail(e.target.value.trim().toLowerCase())}
                required
                autoComplete="email"
                iconLeft={<Mail size={14} />}
              />
              {error && <p className="auth-panel__error">{error}</p>}
              <Button
                type="submit"
                variant="primary"
                size="md"
                loading={loading}
                disabled={loading}
                style={{ width: '100%' }}
              >
                Send reset code
              </Button>
            </form>
          ) : (
            <form className="auth-panel__form" onSubmit={handleForgotReset}>
              <p className="auth-panel__forgot-desc">
                We sent a 6-digit code to <strong>{forgotEmail}</strong>. Enter it below along with your new password.
              </p>
              <Input
                label="One-time code"
                type="text"
                placeholder="123456"
                value={otp}
                onChange={e => setOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
                required
                autoComplete="one-time-code"
                iconLeft={<KeyRound size={14} />}
                hint="Check your inbox (and spam folder)."
              />
              <Input
                label="New password"
                type={showNewPassword ? 'text' : 'password'}
                placeholder="••••••••"
                value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                required
                autoComplete="new-password"
                iconLeft={<Lock size={14} />}
                iconRight={
                  <button
                    type="button"
                    className="auth-panel__pw-toggle"
                    onClick={() => setShowNewPassword(v => !v)}
                    aria-label={showNewPassword ? 'Hide password' : 'Show password'}
                  >
                    {showNewPassword ? <EyeOff size={14} /> : <Eye size={14} />}
                  </button>
                }
                hint="8+ characters with letters and numbers."
              />
              {error && <p className="auth-panel__error">{error}</p>}
              <Button
                type="submit"
                variant="primary"
                size="md"
                loading={loading}
                disabled={loading}
                style={{ width: '100%' }}
              >
                Reset password
              </Button>
              <button
                type="button"
                className="auth-panel__resend"
                onClick={() => { setForgotStep(1); setError(''); setOtp('') }}
              >
                Didn't receive the code? Go back
              </button>
            </form>
          )}
        </div>
      </div>
    )
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
        {registerMode && registerStep === 'role' ? (
          <div className="auth-panel__role-step">
            <p className="auth-panel__role-heading">How will you use Aztea?</p>
            <div className="auth-panel__role-cards">
              <button
                type="button"
                className={`auth-panel__role-card ${selectedRole === 'hirer' ? 'auth-panel__role-card--selected' : ''}`}
                onClick={() => setSelectedRole('hirer')}
              >
                <span className="auth-panel__role-card-icon"><Zap size={22} /></span>
                <strong>I hire agents</strong>
                <span>Delegate tasks, get results. $2 free credit to start.</span>
              </button>
              <button
                type="button"
                className={`auth-panel__role-card ${selectedRole === 'builder' ? 'auth-panel__role-card--selected' : ''}`}
                onClick={() => setSelectedRole('builder')}
              >
                <span className="auth-panel__role-card-icon"><Hammer size={22} /></span>
                <strong>I build agents</strong>
                <span>List your skills and earn revenue per task.</span>
              </button>
            </div>
            <Button
              type="button"
              variant="primary"
              size="md"
              disabled={!selectedRole}
              onClick={() => setRegisterStep('form')}
              style={{ width: '100%' }}
            >
              Continue
            </Button>
            <p className="auth-panel__hint">Already have an account? Sign in above.</p>
          </div>
        ) : (
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
            <Button
              type="submit"
              variant="primary"
              size="md"
              loading={loading}
              disabled={loading}
              aria-disabled={!canSubmit}
              style={{ width: '100%' }}
            >
              {tab === 'signin' ? 'Sign in' : 'Create account'}
            </Button>
            {tab === 'signin' && (
              <button
                type="button"
                className="auth-panel__forgot-link"
                onClick={() => switchTab('forgot')}
              >
                Forgot password?
              </button>
            )}
          <p className="auth-panel__hint">
            {tab === 'signin'
              ? 'New here? Switch to "Create account" above.'
              : 'Already have an account? Sign in above.'}
          </p>
        </form>
        )}
      </div>
    </div>
  )
}
