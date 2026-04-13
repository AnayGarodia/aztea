import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { MarketProvider } from './context/MarketContext'
import LandingPage from './components/LandingPage'
import Dashboard from './components/Dashboard'

export default function App() {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('agentmarket_key') ?? '')
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem('agentmarket_user') ?? 'null') } catch { return null }
  })
  const [view, setView] = useState(apiKey ? 'dashboard' : 'landing')

  const handleConnect = (key, userInfo) => {
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
