import { useState } from 'react'
import './Ripple.css'

export default function Ripple({ children, color = 'var(--accent-glow)', className, style, onClick }) {
  const [ripples, setRipples] = useState([])

  const handleClick = (e) => {
    const el = e.currentTarget
    const rect = el.getBoundingClientRect()
    const x = e.clientX - rect.left
    const y = e.clientY - rect.top
    const id = performance.now()
    setRipples(r => [...r.slice(-4), { id, x, y }])
    setTimeout(() => setRipples(r => r.filter(rp => rp.id !== id)), 700)
    onClick?.(e)
  }

  return (
    <div
      className={`ripple-wrap ${className ?? ''}`}
      style={style}
      onClick={handleClick}
    >
      {children}
      {ripples.map(rp => (
        <span
          key={rp.id}
          className="ripple-wave"
          style={{
            left: rp.x,
            top: rp.y,
            background: color,
          }}
          aria-hidden
        />
      ))}
    </div>
  )
}
