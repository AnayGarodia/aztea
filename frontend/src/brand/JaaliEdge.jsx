import './JaaliEdge.css'

// Jaali-inspired vertical edge pattern: tessellated diamond lattice in subtle terracotta.
// Modern architectural screenwork, very low opacity — sits at the far edge of marketing pages.
export default function JaaliEdge({ side = 'left', className = '' }) {
  return (
    <div className={`jaali-edge jaali-edge--${side} ${className}`.trim()} aria-hidden="true">
      <svg
        width="44"
        height="100%"
        viewBox="0 0 44 480"
        preserveAspectRatio="xMidYMid"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        <defs>
          <pattern id={`jaali-${side}`} x="0" y="0" width="22" height="32" patternUnits="userSpaceOnUse">
            <path
              d="M11 0L22 16L11 32L0 16L11 0Z"
              stroke="currentColor"
              strokeWidth="0.7"
              fill="none"
              opacity="0.35"
            />
            <circle cx="11" cy="16" r="0.9" fill="currentColor" opacity="0.42" />
            <line x1="0" y1="16" x2="22" y2="16" stroke="currentColor" strokeWidth="0.4" opacity="0.18" />
          </pattern>
        </defs>
        <rect width="44" height="480" fill={`url(#jaali-${side})`} />
      </svg>
    </div>
  )
}
