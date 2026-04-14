import { Outlet } from 'react-router-dom'
import Sidebar from './Sidebar'
import Toast from '../ui/Toast'
import { useMarket } from '../context/MarketContext'
import './AppShell.css'

export default function AppShell() {
  const { toast } = useMarket()
  return (
    <div className="shell">
      <Sidebar />
      <div className="shell__main">
        <Outlet />
      </div>
      <Toast toast={toast} />
    </div>
  )
}
