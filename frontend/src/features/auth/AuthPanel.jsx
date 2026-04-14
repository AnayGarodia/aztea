import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { authLogin, authRegister } from '../../api'
import { useAuth } from '../../context/AuthContext'
import Button from '../../ui/Button'
import Input from '../../ui/Input'
import { Mail, Lock, User } from 'lucide-react'
import './AuthPanel.css'

export default function AuthPanel() {
  const { connect } = useAuth()
  const navigate = useNavigate()
  const [tab, setTab] = useState('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [username, setUsername] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      let result
      if (tab === 'signin') {
        result = await authLogin(email, password)
      } else {
        result = await authRegister(username, email, password)
      }
      const userInfo = {
        user_id: result.user_id,
        username: result.username ?? username,
        email: result.email ?? email,
        scopes: result.scopes ?? ['caller'],
      }
      connect(result.raw_api_key, userInfo)
      navigate('/overview')
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
          onClick={() => { setTab('signin'); setError('') }}
          type="button"
        >
          Sign in
        </button>
        <button
          className={`auth-panel__tab ${tab === 'register' ? 'auth-panel__tab--active' : ''}`}
          onClick={() => { setTab('register'); setError('') }}
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
              required
              autoComplete="username"
              iconLeft={<User size={14} />}
            />
          )}
          <Input
            label="Email"
            type="email"
            placeholder="you@example.com"
            value={email}
            onChange={e => setEmail(e.target.value)}
            required
            autoComplete="email"
            iconLeft={<Mail size={14} />}
          />
          <Input
            label="Password"
            type="password"
            placeholder="••••••••"
            value={password}
            onChange={e => setPassword(e.target.value)}
            required
            autoComplete={tab === 'signin' ? 'current-password' : 'new-password'}
            iconLeft={<Lock size={14} />}
          />
          {error && <p className="auth-panel__error">{error}</p>}
          <Button type="submit" variant="primary" size="md" loading={loading} style={{ width: '100%' }}>
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
