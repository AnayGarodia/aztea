// AzteaBoot — full-screen loading state.
// Three concentric rings of jaali dots rotating in alternating directions,
// an octagram core pulsing in the center, and the Aztea wordmark fading in
// below. Pure CSS animations, no canvas, no framer-motion — GPU-only.
//
// Inspired by 21st.dev's concentric-rings-loader pattern, rebuilt with
// brand tokens and Indian-architectural geometry instead of generic dots.

import './AzteaBoot.css'

function ringDots(count, radius) {
  const dots = []
  for (let i = 0; i < count; i++) {
    const angle = (i / count) * 2 * Math.PI - Math.PI / 2
    const x = Math.cos(angle) * radius
    const y = Math.sin(angle) * radius
    dots.push(<span key={i} className="boot__dot" style={{ transform: `translate(${x}px, ${y}px)` }} />)
  }
  return dots
}

export default function AzteaBoot({ label = 'connecting' }) {
  return (
    <div className="boot" role="status" aria-live="polite">
      <div className="boot__center">
        {/* Outer ring — 16 dots, CCW, 9s */}
        <div className="boot__ring boot__ring--outer">
          {ringDots(16, 88)}
        </div>
        {/* Mid ring — 12 dots, CW, 6s */}
        <div className="boot__ring boot__ring--mid">
          {ringDots(12, 60)}
        </div>
        {/* Inner ring — 8 dots, CCW, 4s */}
        <div className="boot__ring boot__ring--inner">
          {ringDots(8, 36)}
        </div>
        {/* Octagram core */}
        <svg
          className="boot__star"
          viewBox="-20 -20 40 40"
          width="40"
          height="40"
          aria-hidden
        >
          <rect x="-14" y="-14" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <rect x="-14" y="-14" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="1.6" transform="rotate(45)" />
          <circle r="2.6" fill="currentColor" />
        </svg>
        {/* Soft glow */}
        <div className="boot__glow" aria-hidden />
      </div>

      <div className="boot__brand" aria-hidden>
        <span className="boot__word">A</span>
        <span className="boot__word">Z</span>
        <span className="boot__word">T</span>
        <span className="boot__word">E</span>
        <span className="boot__word">A</span>
      </div>
      <p className="boot__label">{label}</p>
    </div>
  )
}
