import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { useMarket } from '../context/MarketContext'
import './Topbar.css'

function fmtBalance(cents) {
  if (cents == null) return '--'
  return '$' + (cents / 100).toFixed(2)
}

export default function Topbar({ crumbs = [] }) {
  const { wallet } = useMarket()
  const balance = wallet?.balance_cents ?? null
  const low = balance != null && balance < 50

  return (
    <header className="topbar">
      <nav className="topbar__breadcrumb" aria-label="Breadcrumb">
        {crumbs.map((c, i) => (
          <span key={i} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            {i > 0 && <ChevronRight size={13} />}
            {c.to
              ? <Link to={c.to}>{c.label}</Link>
              : <span className="topbar__breadcrumb-current">{c.label}</span>
            }
          </span>
        ))}
      </nav>

      <div className="topbar__actions">
        <Link
          to="/wallet"
          className={`topbar__balance ${low ? 'topbar__balance--low' : ''}`}
          aria-label={`Wallet balance: ${fmtBalance(balance)}`}
        >
          <span className="topbar__balance-dot" />
          {fmtBalance(balance)}
        </Link>
      </div>
    </header>
  )
}
