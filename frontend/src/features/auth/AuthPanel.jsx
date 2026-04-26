import { useEffect, useRef, useState } from 'react'
import { useNavigate, useLocation, useSearchParams } from 'react-router-dom'
import { authLogin, authGoogle, authSignupStart, authSignupVerify, authSignupResend, authForgotPassword, authResetPassword } from '../../api'
import { useAuth } from '../../context/AuthContext'
import Button from '../../ui/Button'
import Input from '../../ui/Input'
import { Mail, Lock, User, Eye, EyeOff, KeyRound, ArrowLeft } from 'lucide-react'
import './AuthPanel.css'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
const GOOGLE_CLIENT_ID = (import.meta.env.VITE_GOOGLE_CLIENT_ID ?? '').trim()

function GoogleSignInButton({ tab, onCredential }) {
  const containerRef = useRef(null)
  const callbackRef = useRef(onCredential)
  callbackRef.current = onCredential

  useEffect(() => {
    if (!GOOGLE_CLIENT_ID) return
    let cancelled = false
    let pollId = null

    const render = () => {
      if (cancelled) return
      const gid = window.google?.accounts?.id
      if (!gid || !containerRef.current) return false
      try {
        gid.initialize({
          client_id: GOOGLE_CLIENT_ID,
          callback: (response) => {
            const token = response?.credential
            if (token) callbackRef.current?.(token)
          },
          ux_mode: 'popup',
          auto_select: false,
        })
        containerRef.current.innerHTML = ''
        gid.renderButton(containerRef.current, {
          theme: 'outline',
          size: 'large',
          width: 320,
          text: tab === 'register' ? 'signup_with' : 'signin_with',
          logo_alignment: 'center',
        })
      } catch {
        // GIS sometimes throws on first render in dev; retry next tick.
        return false
      }
      return true
    }

    if (!render()) {
      pollId = setInterval(() => {
        if (render()) {
          clearInterval(pollId)
          pollId = null
        }
      }, 200)
    }
    return () => {
      cancelled = true
      if (pollId) clearInterval(pollId)
    }
  }, [tab])

  if (!GOOGLE_CLIENT_ID) return null
  return (
    <div className="auth-panel__google">
      <div ref={containerRef} className="auth-panel__google-btn" />
      <div className="auth-panel__divider"><span>or</span></div>
    </div>
  )
}

export default function AuthPanel() {
  const { connect } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  // Redirect destination is held in a ref so it can be updated by the
  // landing-page CTAs (which dispatch aztea:auth-tab events with a redirect
  // payload) without forcing a re-mount or mutating the URL — URL mutation
  // mid-form is what caused the previous "List an Agent" reset bug.
  const redirectRef = useRef(
    searchParams.get('redirect')
    ?? (location.state?.from && location.state.from !== '/welcome' ? location.state.from : '/overview')
  )
  // Keep ref in sync if the URL itself supplies a fresher redirect value
  // (e.g. RequireLegalAcceptance bouncing the user from a protected route).
  const urlRedirect = searchParams.get('redirect')
  if (urlRedirect && urlRedirect !== redirectRef.current) redirectRef.current = urlRedirect
  const redirectTo = redirectRef.current
  const [tab, setTab] = useState(() => searchParams.get('tab') === 'register' ? 'register' : 'signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [username, setUsername] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [showConfirmPassword, setShowConfirmPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // Role is no longer chosen up front — every account starts as 'both' and
  // users pick their focus later from Settings. This keeps signup to one
  // short form with no extra "How will you use Aztea?" step.
  const [registerStep, setRegisterStep] = useState('form') // 'form' | 'verify'
  const selectedRole = 'both'

  // Signup OTP state
  const [signupOtp, setSignupOtp] = useState('')
  const [resending, setResending] = useState(false)
  const [resentNote, setResentNote] = useState('')

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
      setRegisterStep('form')
      setSignupOtp('')
      setResentNote('')
    }
    if (nextTab === 'forgot') {
      setForgotStep(1)
      setForgotEmail(email)
      setOtp('')
      setNewPassword('')
      setForgotSuccess(false)
    }
  }

  // Use a ref so the event listener always calls the latest switchTab
  // without needing to re-register on every render.
  const switchTabRef = useRef(null)
  switchTabRef.current = switchTab

  useEffect(() => {
    const handler = (event) => {
      const next = event?.detail?.tab
      const nextRedirect = event?.detail?.redirect
      if (nextRedirect) redirectRef.current = nextRedirect
      if (next === 'signin' || next === 'register') switchTabRef.current(next)
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
      if (!registerMode) {
        const result = await authLogin(normalizedEmail, password)
        const userInfo = buildUserInfo(result, normalizedEmail, selectedRole)
        connect(result.raw_api_key, userInfo)
        // `replace` so the user can't hit Back into /welcome after signing in
        // — this also prevents RequireLegalAcceptance from re-bouncing them
        // back to /welcome if it re-evaluates before apiKey propagates.
        navigate(redirectTo, { replace: true })
      } else {
        // Register: kick off email-OTP signup. We do NOT create an account
        // until /auth/signup/verify succeeds.
        await authSignupStart(normalizedUsername, normalizedEmail, password, selectedRole || 'both')
        setSignupOtp('')
        setResentNote('')
        setRegisterStep('verify')
      }
    } catch (err) {
      setError(err.message ?? 'Authentication failed')
    } finally {
      setLoading(false)
    }
  }

  const handleGoogleCredential = async (idToken) => {
    if (loading) return
    setError('')
    setLoading(true)
    try {
      const result = await authGoogle(idToken)
      const userInfo = buildUserInfo(result, result?.email ?? '', result?.role ?? 'both')
      if (result?.user_id) {
        // Re-show onboarding for any first-time Google account.
        localStorage.removeItem(`aztea_onboarding_done:${result.user_id}`)
      }
      connect(result.raw_api_key, userInfo)
      navigate(redirectTo, { replace: true })
    } catch (err) {
      setError(err.message ?? 'Google sign-in failed. Try again.')
    } finally {
      setLoading(false)
    }
  }

  const buildUserInfo = (result, fallbackEmail, fallbackRole) => ({
    user_id: result.user_id,
    username: result.username ?? normalizedUsername,
    email: result.email ?? fallbackEmail,
    role: result.role ?? fallbackRole ?? 'both',
    scopes: result.scopes ?? ['caller'],
    legal_acceptance_required: Boolean(result.legal_acceptance_required),
    legal_accepted_at: result.legal_accepted_at ?? null,
    terms_version_current: result.terms_version_current ?? null,
    privacy_version_current: result.privacy_version_current ?? null,
    terms_version_accepted: result.terms_version_accepted ?? null,
    privacy_version_accepted: result.privacy_version_accepted ?? null,
  })

  const handleVerifySignup = async (e) => {
    e.preventDefault()
    if (loading) return
    setError('')
    if (signupOtp.trim().length !== 6) {
      setError('Enter the 6-digit code from your email.')
      return
    }
    setLoading(true)
    try {
      const result = await authSignupVerify(normalizedEmail, signupOtp.trim())
      const userInfo = buildUserInfo(result, normalizedEmail, selectedRole)
      if (result.user_id) {
        localStorage.removeItem(`aztea_onboarding_done:${result.user_id}`)
      }
      connect(result.raw_api_key, userInfo)
      navigate(redirectTo)
    } catch (err) {
      setError(err.message ?? 'Verification failed')
    } finally {
      setLoading(false)
    }
  }

  const handleResendSignupOtp = async () => {
    if (resending) return
    setError('')
    setResentNote('')
    setResending(true)
    try {
      await authSignupResend(normalizedEmail)
      setResentNote('A new code is on its way.')
    } catch (err) {
      setError(err.message ?? 'Could not resend code.')
    } finally {
      setResending(false)
    }
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
        {registerMode && registerStep === 'verify' ? (
          <form className="auth-panel__form" onSubmit={handleVerifySignup}>
            <p className="auth-panel__forgot-desc">
              We sent a 6-digit code to <strong>{normalizedEmail}</strong>. Enter it below to finish creating your account.
            </p>
            <Input
              label="Verification code"
              type="text"
              placeholder="123456"
              value={signupOtp}
              onChange={e => setSignupOtp(e.target.value.replace(/\D/g, '').slice(0, 6))}
              required
              autoComplete="one-time-code"
              iconLeft={<KeyRound size={14} />}
              hint="Check your inbox (and spam folder)."
            />
            {error && <p className="auth-panel__error">{error}</p>}
            {resentNote && !error && (
              <p className="auth-panel__hint" style={{ color: 'var(--positive)' }}>{resentNote}</p>
            )}
            <Button
              type="submit"
              variant="primary"
              size="md"
              loading={loading}
              disabled={loading}
              style={{ width: '100%' }}
            >
              Verify & continue
            </Button>
            <button
              type="button"
              className="auth-panel__resend"
              onClick={handleResendSignupOtp}
              disabled={resending}
            >
              {resending ? 'Sending…' : "Didn't get it? Resend code"}
            </button>
            <button
              type="button"
              className="auth-panel__resend"
              onClick={() => { setRegisterStep('form'); setError('') }}
            >
              Use a different email
            </button>
          </form>
        ) : (
        <form className="auth-panel__form" onSubmit={handleSubmit}>
          <GoogleSignInButton tab={tab} onCredential={handleGoogleCredential} />
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
              {tab === 'signin' ? 'Sign in' : 'Send verification code'}
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
