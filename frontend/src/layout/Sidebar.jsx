import { NavLink, useNavigate } from 'react-router-dom'
import { LayoutDashboard, Bot, Briefcase, Wallet, Settings, LogOut } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { useMarket } from '../context/MarketContext'
import Avatar from '../ui/Avatar'
import AgentAvatar from '../brand/AgentAvatar'
import './Sidebar.css'

const NAV = [
  { to: '/overview', icon: <LayoutDashboard size={16} />, label: 'Overview' },
  { to: '/agents',   icon: <Bot size={16} />,             label: 'Agents' },
  { to: '/jobs',     icon: <Briefcase size={16} />,       label: 'Jobs' },
  { to: '/wallet',   icon: <Wallet size={16} />,          label: 'Wallet' },
  { to: '/settings', icon: <Settings size={16} />,        label: 'Settings' },
]

export default function Sidebar() {
  const { user, disconnect } = useAuth()
  const { agents = [] } = useMarket()
  const navigate = useNavigate()

  const handleSignOut = () => {
    disconnect()
    navigate('/welcome')
  }

  return (
    <aside className="sidebar">
      <NavLink to="/overview" className="sidebar__brand">
        <div className="sidebar__logo">AM</div>
        <span className="sidebar__wordmark">agentmarket</span>
      </NavLink>

      <nav className="sidebar__nav">
        {NAV.map(({ to, icon, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) =>
              `sidebar__link ${isActive ? 'sidebar__link--active' : ''}`
            }
          >
            {icon}
            {label}
          </NavLink>
        ))}

        {agents.length > 0 && (
          <div className="sidebar__agents-strip">
            <p className="sidebar__agents-title">City pulse</p>
            <div className="sidebar__agents-list">
              {agents.slice(0, 4).map((agent) => (
                <button
                  key={agent.agent_id}
                  type="button"
                  className="sidebar__agent-chip"
                  onClick={() => navigate(`/agents/${agent.agent_id}`)}
                  title={agent.name}
                >
                  <AgentAvatar name={agent.name} size="xs" />
                  <span>{agent.name}</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </nav>

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
          <p className="sidebar__email">{user?.email ?? ''}</p>
        </div>
        <button
          className="sidebar__signout"
          onClick={(e) => { e.stopPropagation(); handleSignOut() }}
          aria-label="Sign out"
          title="Sign out"
        >
          <LogOut size={14} />
        </button>
      </div>
    </aside>
  )
}
