import { NavLink, useNavigate, useLocation } from 'react-router-dom'
import { motion, AnimatePresence } from 'motion/react'
import {
  LayoutDashboard, Bot, Briefcase, Hammer, Wallet, Settings, LogOut, Sun, Moon
} from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { useMarket } from '../context/MarketContext'
import { useTheme } from '../context/ThemeContext'
import AgentSigil from '../brand/AgentSigil'
import Avatar from '../ui/Avatar'
import './Sidebar.css'

const NAV = [
  { to: '/overview', icon: LayoutDashboard, label: 'Overview' },
  { to: '/agents',   icon: Bot,             label: 'Discover' },
  { to: '/jobs',     icon: Briefcase,       label: 'Jobs' },
  { to: '/worker',   icon: Hammer,          label: 'Worker' },
  { to: '/wallet',   icon: Wallet,          label: 'Wallet' },
  { to: '/settings', icon: Settings,        label: 'Settings' },
]

export default function Sidebar() {
  const { user, disconnect } = useAuth()
  const { agents = [] } = useMarket()
  const { toggle, isDark } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()

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
        {NAV.map(({ to, icon: Icon, label }) => {
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

      {/* Live agents strip */}
      {agents.length > 0 && (
        <div className="sidebar__agents">
          <p className="sidebar__section-label">Live agents</p>
          <div className="sidebar__agents-list">
            {agents.slice(0, 5).map((agent) => (
              <button
                key={agent.agent_id}
                type="button"
                className="sidebar__agent-chip"
                onClick={() => navigate(`/agents/${agent.agent_id}`)}
                title={agent.name}
              >
                <AgentSigil agentId={agent.agent_id} size="xs" />
                <span className="sidebar__agent-name">{agent.name}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Footer */}
      <div className="sidebar__footer">
        <div className="sidebar__legal-links">
          <NavLink to="/terms" className="sidebar__legal-link">Terms</NavLink>
          <span className="sidebar__legal-sep">·</span>
          <NavLink to="/privacy" className="sidebar__legal-link">Privacy</NavLink>
          <span className="sidebar__legal-sep">·</span>
          <NavLink to="/docs" className="sidebar__legal-link">Docs</NavLink>
        </div>

        <button
          className="sidebar__theme-btn"
          onClick={toggle}
          title={isDark ? 'Switch to light' : 'Switch to dark'}
          aria-label="Toggle theme"
        >
          <AnimatePresence mode="wait" initial={false}>
            <motion.span
              key={isDark ? 'moon' : 'sun'}
              initial={{ opacity: 0, rotate: -30, scale: 0.8 }}
              animate={{ opacity: 1, rotate: 0, scale: 1 }}
              exit={{ opacity: 0, rotate: 30, scale: 0.8 }}
              transition={{ duration: 0.2 }}
            >
              {isDark ? <Sun size={14} /> : <Moon size={14} />}
            </motion.span>
          </AnimatePresence>
        </button>

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
