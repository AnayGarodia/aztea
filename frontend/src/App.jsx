import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { MarketProvider } from './context/MarketContext'
import { authMe } from './api'
import LandingPage from './components/LandingPage'
import Dashboard from './components/Dashboard'

export default function App() {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('agentmarket_key') ?? '')
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem('agentmarket_user') ?? 'null') } catch { return null }
  })
  const [view, setView] = useState(apiKey ? 'dashboard' : 'landing')
  const [booting, setBooting] = useState(true)

  useEffect(() => {
    let active = true
    const bootstrap = async () => {
      if (!apiKey) {
        if (!active) return
        setView('landing')
        setBooting(false)
        return
      }
      setBooting(true)
      try {
        const profile = await authMe(apiKey)
        if (!active) return
        const mergedUser = {
          user_id: profile.user_id ?? user?.user_id,
          username: profile.username ?? user?.username ?? 'Agent',
          email: profile.email ?? user?.email ?? '',
          scopes: profile.scopes ?? user?.scopes ?? [],
        }
        localStorage.setItem('agentmarket_user', JSON.stringify(mergedUser))
        setUser(mergedUser)
        setView('dashboard')
      } catch {
        if (!active) return
        localStorage.removeItem('agentmarket_key')
        localStorage.removeItem('agentmarket_user')
        setApiKey('')
        setUser(null)
        setView('landing')
      } finally {
        if (active) setBooting(false)
      }
    }
    bootstrap()
    return () => { active = false }
  }, [apiKey])

  const handleConnect = (key, userInfo) => {
    localStorage.setItem('agentmarket_key', key)
    if (userInfo) localStorage.setItem('agentmarket_user', JSON.stringify(userInfo))
    setApiKey(key)
    if (userInfo) setUser(userInfo)
    setView('dashboard')
  }

  const handleSignOut = () => {
    localStorage.removeItem('agentmarket_key')
    localStorage.removeItem('agentmarket_user')
    setApiKey('')
    setUser(null)
    setView('landing')
  }

  if (booting) {
    return (
      <div style={{
        minHeight: '100vh',
        background: 'var(--bg)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'var(--text-muted)',
        fontFamily: 'var(--font-sans)',
      }}>
        Connecting to marketplace…
      </div>
    )
  }

  return (
    <AnimatePresence mode="wait">
      {view === 'landing' ? (
        <motion.div
          key="landing"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.22 }}
        >
          <LandingPage onEnterDashboard={handleConnect} />
        </motion.div>
      ) : (
        <motion.div
          key="dashboard"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.22 }}
          style={{ height: '100vh' }}
        >
          <MarketProvider apiKey={apiKey}>
            <Dashboard onSignOut={handleSignOut} user={user} />
          </MarketProvider>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
