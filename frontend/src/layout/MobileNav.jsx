import { NavLink, useLocation } from 'react-router-dom'
import { LayoutDashboard, Bot, Briefcase, Wallet, BookOpen, Settings } from 'lucide-react'
import './MobileNav.css'

const NAV = [
  { to: '/overview', icon: LayoutDashboard, label: 'Home' },
  { to: '/agents',   icon: Bot,             label: 'Discover' },
  { to: '/jobs',     icon: Briefcase,       label: 'Jobs' },
  { to: '/wallet',   icon: Wallet,          label: 'Wallet' },
  { to: '/docs',     icon: BookOpen,        label: 'Docs' },
  { to: '/settings', icon: Settings,        label: 'Settings' },
]

export default function MobileNav() {
  const location = useLocation()
  return (
    <nav className="mobile-nav">
      {NAV.map(({ to, icon: Icon, label }) => {
        const isActive = location.pathname === to || (to !== '/overview' && location.pathname.startsWith(to))
        return (
          <NavLink key={to} to={to} className={`mobile-nav__item ${isActive ? 'mobile-nav__item--active' : ''}`}>
            <Icon size={20} />
            <span>{label}</span>
          </NavLink>
        )
      })}
    </nav>
  )
}
