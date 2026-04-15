import AgentCharacter from '../brand/AgentCharacter'
import { generateAgentCharacter } from '../brand/characterUtils'
import './EmptyState.css'

// Fixed mascot traits for empty/loading states
const EMPTY_TRAITS = generateAgentCharacter('empty-state-mascot')
const LOADING_TRAITS = generateAgentCharacter('loading-state-mascot')

export function LoadingCharacter({ message = 'Loading…' }) {
  return (
    <div className="empty">
      <div className="empty__loading-wrap">
        <div className="char-ring empty__loading-ring" />
        <AgentCharacter {...LOADING_TRAITS} state="working" size={64} />
      </div>
      <p className="empty__title">{message}</p>
    </div>
  )
}

export default function EmptyState({ icon, title, sub, action }) {
  return (
    <div className="empty">
      <div className="empty__mascot-wrap">
        <AgentCharacter {...EMPTY_TRAITS} state="idle" size={64} />
        {title && (
          <div className="empty__bubble">
            {title}
          </div>
        )}
      </div>
      {sub && <p className="empty__sub">{sub}</p>}
      {action}
    </div>
  )
}
