import './EmptyState.css'

export default function EmptyState({ icon, title, sub, action }) {
  return (
    <div className="empty">
      {icon && <div className="empty__icon">{icon}</div>}
      {title && <p className="empty__title">{title}</p>}
      {sub && <p className="empty__sub">{sub}</p>}
      {action}
    </div>
  )
}
