import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { useMarket } from '../context/MarketContext'
import './Topbar.css'

function fmtBalance(cents) {
  if (cents == null) return '--'
  return '$' + (cents / 100).toFixed(2)
}

export default function Topbar({ crumbs = [] }) {
  const market = useMarket()
  const balance = market?.wallet?.balance_cents ?? null
  const low = balance != null && balance < 50

  return (
    <header className="topbar">
      <nav className="topbar__breadcrumb" aria-label="Breadcrumb">
        {crumbs.map((c, i) => (
          <span key={i} className="topbar__crumb-wrap">
            {i > 0 && <ChevronRight size={12} className="topbar__crumb-sep" />}
            {c.to
              ? <Link to={c.to} className="topbar__crumb-link">{c.label}</Link>
              : <span className="topbar__crumb-current">{c.label}</span>
            }
          </span>
        ))}
      </nav>

      {market && (
        <div className="topbar__actions">
          <Link
            to="/wallet"
            className={`topbar__balance ${low ? 'topbar__balance--low' : ''}`}
            aria-label={`Wallet balance: ${fmtBalance(balance)}`}
          >
            <span className="topbar__balance-dot" />
            <span className="topbar__balance-label">Balance</span>
            <span className="topbar__balance-value">{fmtBalance(balance)}</span>
          </Link>
        </div>
      )}
    </header>
  )
}
