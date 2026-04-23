import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import { MarketProvider } from './context/MarketContext'
import { ThemeProvider } from './context/ThemeContext'
import ErrorBoundary from './ui/ErrorBoundary'

// Landing is eagerly imported because cold-start always lands on `/welcome`
// when the user is unauthenticated. Everything else lazy-loads per route so
// the initial JS payload stays small.
import LandingPage from './pages/LandingPage'

const AppShell = lazy(() => import('./layout/AppShell'))
const OnboardingWizard = lazy(() => import('./features/onboarding/OnboardingWizard'))
const DocsPage = lazy(() => import('./pages/DocsPage'))
const TermsPage = lazy(() => import('./pages/TermsPage'))
const PrivacyPage = lazy(() => import('./pages/PrivacyPage'))
const LegalAcceptancePage = lazy(() => import('./pages/LegalAcceptancePage'))
const DashboardPage = lazy(() => import('./pages/DashboardPage'))
const AgentsPage = lazy(() => import('./pages/AgentsPage'))
const AgentDetailPage = lazy(() => import('./pages/AgentDetailPage'))
const JobsPage = lazy(() => import('./pages/JobsPage'))
const JobDetailPage = lazy(() => import('./pages/JobDetailPage'))
const WorkerPage = lazy(() => import('./pages/WorkerPage'))
const WalletPage = lazy(() => import('./pages/WalletPage'))
const SettingsPage = lazy(() => import('./pages/SettingsPage'))
const AdminDisputesPage = lazy(() => import('./pages/AdminDisputesPage'))
const MyAgentsPage = lazy(() => import('./pages/MyAgentsPage'))
const RegisterAgentPage = lazy(() => import('./pages/RegisterAgentPage'))
const PlatformPage = lazy(() => import('./pages/PlatformPage'))

function RequireAuth({ children }) {
  const { apiKey, booting } = useAuth()
  if (booting) return <AppBoot />
  if (!apiKey) return <Navigate to="/welcome" replace />
  return children
}

function RequireAdmin({ children }) {
  const { user } = useAuth()
  if (!user?.scopes?.includes('admin')) {
    return (
      <main className="app-gate">Admin access required.</main>
    )
  }
  return children
}

function RequireLegalAcceptance({ children }) {
  const { apiKey, booting, user } = useAuth()
  const location = useLocation()
  if (booting) return <AppBoot />
  if (!apiKey) return <Navigate to="/welcome" replace />
  if (user?.legal_acceptance_required) {
    return <Navigate to="/legal/accept" replace state={{ from: location.pathname }} />
  }
  return children
}

function AppBoot() {
  return <div className="app-boot">connecting…</div>
}

function AuthedApp() {
  const { apiKey } = useAuth()
  return (
    <MarketProvider apiKey={apiKey}>
      <Suspense fallback={null}>
        <OnboardingWizard />
      </Suspense>
      <ErrorBoundary>
        <Suspense fallback={<AppBoot />}>
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
              <Route path="/my-agents" element={<MyAgentsPage />} />
              <Route path="/register-agent" element={<RegisterAgentPage />} />
              <Route path="/platform" element={<PlatformPage />} />
              <Route path="/admin/disputes" element={<RequireAdmin><AdminDisputesPage /></RequireAdmin>} />
              <Route path="*"         element={<Navigate to="/overview" replace />} />
            </Route>
          </Routes>
        </Suspense>
      </ErrorBoundary>
    </MarketProvider>
  )
}

function RootRedirect() {
  const { apiKey, booting, user } = useAuth()
  if (booting) return <AppBoot />
  if (apiKey && user?.legal_acceptance_required) return <Navigate to="/legal/accept" replace />
  return <Navigate to={apiKey ? '/overview' : '/welcome'} replace />
}

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <AuthProvider>
          <ErrorBoundary>
            <Suspense fallback={<AppBoot />}>
              <Routes>
                <Route path="/welcome" element={<LandingPage />} />
                <Route path="/docs"    element={<DocsPage />} />
                <Route path="/docs/:docSlug" element={<DocsPage />} />
                <Route path="/terms"   element={<TermsPage />} />
                <Route path="/privacy" element={<PrivacyPage />} />
                <Route
                  path="/legal/accept"
                  element={
                    <RequireAuth>
                      <LegalAcceptancePage />
                    </RequireAuth>
                  }
                />
                <Route path="/" element={<RootRedirect />} />
                <Route
                  path="/*"
                  element={
                    <RequireLegalAcceptance>
                      <AuthedApp />
                    </RequireLegalAcceptance>
                  }
                />
              </Routes>
            </Suspense>
          </ErrorBoundary>
        </AuthProvider>
      </BrowserRouter>
    </ThemeProvider>
  )
}
