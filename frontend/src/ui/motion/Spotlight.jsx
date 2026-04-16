import { useRef, useState, useCallback } from 'react'

export default function Spotlight({ children, className, style, color = 'var(--accent-glow)' }) {
  const ref = useRef(null)
  const [pos, setPos] = useState({ x: -9999, y: -9999 })
  const [visible, setVisible] = useState(false)

  const handleMove = useCallback((e) => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    setPos({ x: e.clientX - rect.left, y: e.clientY - rect.top })
    setVisible(true)
  }, [])

  const handleLeave = useCallback(() => setVisible(false), [])

  return (
    <div
      ref={ref}
      className={className}
      style={{ position: 'relative', ...style }}
      onMouseMove={handleMove}
      onMouseLeave={handleLeave}
    >
      <div
        style={{
          pointerEvents: 'none',
          position: 'absolute',
          inset: 0,
          overflow: 'hidden',
          borderRadius: 'inherit',
          transition: 'opacity 0.3s ease',
          opacity: visible ? 1 : 0,
          background: `radial-gradient(400px circle at ${pos.x}px ${pos.y}px, ${color}, transparent 70%)`,
          zIndex: 0,
        }}
      />
      <div style={{ position: 'relative', zIndex: 1 }}>{children}</div>
    </div>
  )
}
