import { useState } from 'react'
import './Tabs.css'

export default function Tabs({ tabs, defaultTab, onChange, children, className = '' }) {
  const [active, setActive] = useState(defaultTab ?? tabs?.[0]?.id)

  const handleChange = (id) => {
    setActive(id)
    onChange?.(id)
  }

  return (
    <div className={`tabs ${className}`}>
      <div className="tabs__list" role="tablist">
        {tabs.map(tab => (
          <button
            key={tab.id}
            role="tab"
            aria-selected={active === tab.id}
            className={`tabs__tab ${active === tab.id ? 'tabs__tab--active' : ''}`}
            onClick={() => handleChange(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="tabs__panel">
        {children(active)}
      </div>
    </div>
  )
}
