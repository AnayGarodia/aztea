import AgentSigil from '../brand/AgentSigil'
import './EmptyState.css'

export function LoadingCharacter({ message = 'Loading…' }) {
  return (
    <div className="empty">
      <div className="empty__loading-wrap">
        <div className="empty__spinner" />
      </div>
      <p className="empty__title">{message}</p>
    </div>
  )
}

export default function EmptyState({ agentId, title, sub, action }) {
  return (
    <div className="empty">
      <div className="empty__sigil-wrap">
        <AgentSigil agentId={agentId ?? 'empty-state'} size="md" state="idle" />
      </div>
      {title && <p className="empty__title">{title}</p>}
      {sub && <p className="empty__sub">{sub}</p>}
      {action && <div className="empty__action">{action}</div>}
    </div>
  )
}
