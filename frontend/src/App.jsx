import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import { MarketProvider } from './context/MarketContext'
import { ThemeProvider } from './context/ThemeContext'
import AppShell from './layout/AppShell'
import ErrorBoundary from './ui/ErrorBoundary'

import LandingPage    from './pages/LandingPage'
import TermsPage      from './pages/TermsPage'
import PrivacyPage    from './pages/PrivacyPage'
import DashboardPage  from './pages/DashboardPage'
import AgentsPage     from './pages/AgentsPage'
import AgentDetailPage from './pages/AgentDetailPage'
import JobsPage       from './pages/JobsPage'
import JobDetailPage  from './pages/JobDetailPage'
import WorkerPage     from './pages/WorkerPage'
import WalletPage     from './pages/WalletPage'
import SettingsPage   from './pages/SettingsPage'

function RequireAuth({ children }) {
  const { apiKey, booting } = useAuth()
  if (booting) return <AppBoot />
  if (!apiKey) return <Navigate to="/welcome" replace />
  return children
}

function AppBoot() {
  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--canvas)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      color: 'var(--text-muted)',
      fontFamily: 'var(--font-mono)',
      fontSize: '0.8125rem',
      letterSpacing: '0.05em',
    }}>
      connecting…
    </div>
  )
}

function AuthedApp() {
  const { apiKey } = useAuth()
  return (
    <MarketProvider apiKey={apiKey}>
      <ErrorBoundary>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/overview" element={<DashboardPage />} />
            <Route path="/agents"   element={<AgentsPage />} />
            <Route path="/agents/:id" element={<AgentDetailPage />} />
            <Route path="/jobs"     element={<JobsPage />} />
            <Route path="/jobs/:id" element={<JobDetailPage />} />
            <Route path="/worker"   element={<WorkerPage />} />
            <Route path="/wallet"   element={<WalletPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/terms"    element={<TermsPage />} />
            <Route path="/privacy"  element={<PrivacyPage />} />
            <Route path="*"         element={<Navigate to="/overview" replace />} />
          </Route>
        </Routes>
      </ErrorBoundary>
    </MarketProvider>
  )
}

function RootRedirect() {
  const { apiKey, booting } = useAuth()
  if (booting) return <AppBoot />
  return <Navigate to={apiKey ? '/overview' : '/welcome'} replace />
}

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <AuthProvider>
          <ErrorBoundary>
            <Routes>
              <Route path="/welcome" element={<LandingPage />} />
              <Route path="/" element={<RootRedirect />} />
              <Route
                path="/*"
                element={
                  <RequireAuth>
                    <AuthedApp />
                  </RequireAuth>
                }
              />
            </Routes>
          </ErrorBoundary>
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  )
}
