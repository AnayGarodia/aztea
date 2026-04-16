import { useRef, useState } from 'react'

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export default function Tilt({ children, className, style, maxDeg = 6, scale = 1.02 }) {
  const ref = useRef(null)
  const [transform, setTransform] = useState('')

  const handleMove = (e) => {
    if (prefersReducedMotion()) return
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    const x = (e.clientX - rect.left) / rect.width - 0.5
    const y = (e.clientY - rect.top) / rect.height - 0.5
    const rotX = (-y * maxDeg).toFixed(2)
    const rotY = (x * maxDeg).toFixed(2)
    setTransform(`perspective(800px) rotateX(${rotX}deg) rotateY(${rotY}deg) scale(${scale})`)
  }

  const handleLeave = () => setTransform('')

  return (
    <div
      ref={ref}
      className={className}
      style={{
        ...style,
        transform,
        transition: transform
          ? 'transform 0.05s linear'
          : 'transform 0.35s cubic-bezier(0.16, 1, 0.3, 1)',
        willChange: 'transform',
      }}
      onMouseMove={handleMove}
      onMouseLeave={handleLeave}
    >
      {children}
    </div>
  )
}
