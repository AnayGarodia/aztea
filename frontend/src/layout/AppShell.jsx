import { Outlet, useLocation } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import Sidebar from './Sidebar'
import Toast from '../ui/Toast'
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
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}
          >
            <Outlet />
          </motion.div>
        </AnimatePresence>
      </div>
      <Toast toast={toast} />
    </div>
  )
}
