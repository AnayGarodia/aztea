import { Outlet, useLocation } from 'react-router-dom'
import { AnimatePresence, motion } from 'motion/react'
import Sidebar from './Sidebar'
import MobileNav from './MobileNav'
import Toast from '../ui/Toast'
import OnboardingWizard from '../features/onboarding/OnboardingWizard'
import { useMarket } from '../context/MarketContext'
import './AppShell.css'

export default function AppShell() {
  const { toast } = useMarket()
  const location = useLocation()

  return (
    <div className="shell">
      <Sidebar />
      <div className="shell__main">
        <AnimatePresence mode="wait">
          <motion.div
            key={location.pathname}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="shell__page"
          >
            <Outlet />
          </motion.div>
        </AnimatePresence>
      </div>
      <MobileNav />
      <Toast toast={toast} />
      <OnboardingWizard />
    </div>
  )
}
