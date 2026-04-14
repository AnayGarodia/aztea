import './Stat.css'

export default function Stat({ label, value, sub, accent, variant, className = '' }) {
  const v = variant ?? (accent ? 'accent' : '')
  return (
    <div className={`stat ${v ? `stat--${v}` : ''} ${className}`}>
      {label && <p className="stat__label">{label}</p>}
      <p className="stat__value">{value}</p>
      {sub && <p className="stat__sub">{sub}</p>}
    </div>
  )
}
