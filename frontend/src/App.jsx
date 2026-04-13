import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { MarketProvider } from './context/MarketContext'
import LandingPage from './components/LandingPage'
import Dashboard from './components/Dashboard'

export default function App() {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('agentmarket_key') ?? '')
  const [view, setView] = useState(apiKey ? 'dashboard' : 'landing')

  const handleConnect = (key) => {
    setApiKey(key)
    setView('dashboard')
  }

  const handleSignOut = () => {
    localStorage.removeItem('agentmarket_key')
    setApiKey('')
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
          transition={{ duration: 0.25 }}
        >
          <LandingPage onEnterDashboard={handleConnect} />
        </motion.div>
      ) : (
        <motion.div
          key="dashboard"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.25 }}
          style={{ height: '100vh' }}
        >
          <MarketProvider apiKey={apiKey}>
            <Dashboard onSignOut={handleSignOut} />
          </MarketProvider>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
