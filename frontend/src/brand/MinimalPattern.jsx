import './MinimalPattern.css'

// Family of low-opacity, hairline SVG backgrounds with Indian-modern
// geometric character. Always absolutely-positioned, decorative only.

// 1. DotGrid — a faint terracotta dotted lattice, fading to canvas at edges.
export function DotGrid({ className = '', spacing = 28, dot = 1.2 }) {
  const id = `dots-${spacing}`
  return (
    <svg
      className={`mp ${className}`.trim()}
      width="100%"
      height="100%"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <pattern id={id} x="0" y="0" width={spacing} height={spacing} patternUnits="userSpaceOnUse">
          <circle cx={spacing / 2} cy={spacing / 2} r={dot / 2} fill="currentColor" opacity="0.55" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// 2. JaaliBand — a horizontal hairline jaali strip. Use as a section divider
//    accent. Only the line work — no filled shapes.
export function JaaliBand({ className = '' }) {
  return (
    <svg
      className={`mp mp--band ${className}`.trim()}
      viewBox="0 0 1200 32"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <g stroke="currentColor" strokeWidth="0.55" fill="none" opacity="0.42">
        {Array.from({ length: 30 }).map((_, i) => {
          const x = i * 40 + 20
          return (
            <g key={i}>
              <path d={`M ${x - 12} 16 L ${x} 4 L ${x + 12} 16 L ${x} 28 Z`} />
              <line x1={x - 12} y1="16" x2={x + 12} y2="16" opacity="0.5" />
            </g>
          )
        })}
        <line x1="0" y1="16" x2="1200" y2="16" opacity="0.18" />
      </g>
    </svg>
  )
}

// 3. CornerRangoli — concentric arcs anchored to a corner. Quiet radial echo
//    behind the headline.
export function CornerRangoli({ className = '', corner = 'tr', size = 360 }) {
  const arcs = [size * 0.26, size * 0.4, size * 0.56, size * 0.72]
  return (
    <svg
      className={`mp mp--rangoli mp--rangoli-${corner} ${className}`.trim()}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      fill="none"
      aria-hidden="true"
    >
      <g stroke="currentColor" fill="none" opacity="0.4">
        {arcs.map((r, i) => (
          <circle
            key={i}
            cx={size}
            cy="0"
            r={r}
            strokeWidth={i === 1 ? 0.7 : 0.45}
            strokeDasharray={i % 2 === 0 ? '1 5' : 'none'}
            opacity={0.5 - i * 0.08}
          />
        ))}
        {/* 8 radial petal lines */}
        {Array.from({ length: 8 }).map((_, i) => {
          const angle = (90 + i * 11.25) * Math.PI / 180
          return (
            <line
              key={i}
              x1={size}
              y1="0"
              x2={size + Math.cos(angle) * size * 0.78}
              y2={Math.sin(angle) * size * 0.78}
              strokeWidth="0.4"
              opacity="0.32"
            />
          )
        })}
      </g>
    </svg>
  )
}

// 4. StepwellLines — concentric stepped squares descending into the page,
//    anchored to a corner. Echoes the baoli / stepwell motif.
export function StepwellLines({ className = '', size = 260 }) {
  const steps = 6
  return (
    <svg
      className={`mp mp--stepwell ${className}`.trim()}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      fill="none"
      aria-hidden="true"
    >
      <g stroke="currentColor" fill="none" opacity="0.42">
        {Array.from({ length: steps }).map((_, i) => {
          const inset = (i + 1) * (size / (steps * 2))
          return (
            <rect
              key={i}
              x={inset}
              y={inset}
              width={size - inset * 2}
              height={size - inset * 2}
              strokeWidth="0.55"
              opacity={0.55 - i * 0.07}
            />
          )
        })}
        <rect x={size / 2 - 3} y={size / 2 - 3} width="6" height="6" stroke="none" fill="currentColor" opacity="0.55" transform={`rotate(45 ${size / 2} ${size / 2})`} />
      </g>
    </svg>
  )
}
