import { NavLink, useNavigate, useLocation } from 'react-router-dom'
import { motion } from 'motion/react'
import {
  LayoutDashboard, Bot, Briefcase, Hammer, Wallet, Settings, LogOut, Shield, ListChecks, BookOpen, KeyRound, Coins, Puzzle, FilePlus
} from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import Avatar from '../ui/Avatar'
import './Sidebar.css'

const HIRER_NAV = [
  { to: '/overview',     icon: LayoutDashboard, label: 'Overview' },
  { to: '/agents',       icon: Bot,             label: 'Discover' },
  { to: '/jobs',         icon: Briefcase,       label: 'Jobs' },
  { to: '/wallet',       icon: Wallet,          label: 'Wallet' },
  { to: '/keys',         icon: KeyRound,        label: 'API Keys' },
  { to: '/docs',         icon: BookOpen,        label: 'Docs' },
  { to: '/integrations', icon: Puzzle,          label: 'Integrations' },
  { to: '/settings',     icon: Settings,        label: 'Settings' },
]

const BUILDER_NAV = [
  { to: '/overview',   icon: LayoutDashboard, label: 'Overview' },
  { to: '/my-agents',  icon: ListChecks,      label: 'My Skills' },
  { to: '/worker',     icon: Hammer,          label: 'Worker' },
  { to: '/list-skill', icon: FilePlus,        label: 'List a Skill' },
  { to: '/keys',           icon: KeyRound,        label: 'API Keys' },
  { to: '/docs',           icon: BookOpen,        label: 'Docs' },
  { to: '/integrations',   icon: Puzzle,          label: 'Integrations' },
  { to: '/settings',       icon: Settings,        label: 'Settings' },
]

const BOTH_NAV = [
  { to: '/overview',       icon: LayoutDashboard, label: 'Overview' },
  { to: '/agents',         icon: Bot,             label: 'Discover' },
  { to: '/jobs',           icon: Briefcase,       label: 'Jobs' },
  { to: '/worker',         icon: Hammer,          label: 'Worker' },
  { to: '/my-agents',      icon: ListChecks,      label: 'My Agents' },
  { to: '/wallet',         icon: Wallet,          label: 'Wallet' },
  { to: '/register-agent', icon: FilePlus,        label: 'List a Skill' },
  { to: '/keys',           icon: KeyRound,        label: 'API Keys' },
  { to: '/docs',           icon: BookOpen,        label: 'Docs' },
  { to: '/integrations',   icon: Puzzle,          label: 'Integrations' },
  { to: '/settings',       icon: Settings,        label: 'Settings' },
]

export default function Sidebar() {
  const { user, disconnect } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const role = user?.role ?? 'both'
  const nav = role === 'hirer' ? HIRER_NAV : role === 'builder' ? BUILDER_NAV : BOTH_NAV

  const handleSignOut = () => {
    disconnect()
    navigate('/welcome')
  }

  return (
    <aside className="sidebar">
      {/* Brand */}
      <NavLink to="/overview" className="sidebar__brand">
        <span className="sidebar__wordmark">Aztea</span>
      </NavLink>

      {/* Nav */}
      <nav className="sidebar__nav">
        {nav.map(({ to, icon: Icon, label }) => {
          const isActive = location.pathname === to || (to !== '/overview' && location.pathname.startsWith(to))
          return (
            <NavLink
              key={to}
              to={to}
              className="sidebar__link-wrap"
            >
              <div className={`sidebar__link ${isActive ? 'sidebar__link--active' : ''}`}>
                {isActive && (
                  <motion.div
                    layoutId="sidebar-active"
                    className="sidebar__link-bg"
                    transition={{ type: 'spring', bounce: 0.2, duration: 0.4 }}
                  />
                )}
                <Icon size={16} className="sidebar__link-icon" />
                <span className="sidebar__link-label">{label}</span>
              </div>
            </NavLink>
          )
        })}
      </nav>

      {/* Admin links */}
      {user?.scopes?.includes('admin') && (
        <nav className="sidebar__nav sidebar__nav--admin">
          <NavLink to="/admin/disputes" className="sidebar__link-wrap">
            {({ isActive }) => (
              <div className={`sidebar__link ${isActive ? 'sidebar__link--active' : ''}`}>
                <Shield size={16} className="sidebar__link-icon" />
                <span className="sidebar__link-label">Disputes</span>
              </div>
            )}
          </NavLink>
          <NavLink to="/admin/earnings" className="sidebar__link-wrap">
            {({ isActive }) => (
              <div className={`sidebar__link ${isActive ? 'sidebar__link--active' : ''}`}>
                <Coins size={16} className="sidebar__link-icon" />
                <span className="sidebar__link-label">Platform Earnings</span>
              </div>
            )}
          </NavLink>
        </nav>
      )}

      {/* Footer */}
      <div className="sidebar__footer">
        <div className="sidebar__legal-links">
          <NavLink to="/terms" className="sidebar__legal-link">Terms</NavLink>
          <span className="sidebar__legal-sep">·</span>
          <NavLink to="/privacy" className="sidebar__legal-link">Privacy</NavLink>
        </div>

        <div
          className="sidebar__user"
          onClick={() => navigate('/settings')}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => e.key === 'Enter' && navigate('/settings')}
        >
          <Avatar name={user?.username ?? '?'} size="sm" />
          <div className="sidebar__user-info">
            <p className="sidebar__username">{user?.username ?? 'Agent'}</p>
            <p className="sidebar__useremail">{user?.email ?? ''}</p>
          </div>
          <button
            className="sidebar__signout"
            onClick={(e) => { e.stopPropagation(); handleSignOut() }}
            aria-label="Sign out"
            title="Sign out"
          >
            <LogOut size={13} />
          </button>
        </div>
      </div>
    </aside>
  )
}
