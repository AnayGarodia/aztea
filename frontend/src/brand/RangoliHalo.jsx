import './RangoliHalo.css'

// Mandala/rangoli routing halo: thin radial geometry behind the central
// AZTEA marketplace node. Symmetrical, low-opacity, terracotta + copper
// concentric petals + outer dotted ring.
export default function RangoliHalo({ className = '', size = 320 }) {
  const r1 = size * 0.16
  const r2 = size * 0.26
  const r3 = size * 0.38
  const r4 = size * 0.48
  return (
    <svg
      className={`rangoli-halo ${className}`.trim()}
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <g style={{ transformOrigin: 'center', animation: 'rangoli-spin 80s linear infinite' }}>
        {/* Inner ring + petal points */}
        <circle cx={size / 2} cy={size / 2} r={r1} stroke="currentColor" strokeWidth="0.6" opacity="0.42" />
        <circle cx={size / 2} cy={size / 2} r={r2} stroke="currentColor" strokeWidth="0.5" opacity="0.32" strokeDasharray="2 4" />

        {/* 12 petal lines radiating */}
        {Array.from({ length: 12 }).map((_, i) => {
          const angle = (i * 30 * Math.PI) / 180
          const cx = size / 2
          const cy = size / 2
          const x1 = cx + Math.cos(angle) * r2
          const y1 = cy + Math.sin(angle) * r2
          const x2 = cx + Math.cos(angle) * r3
          const y2 = cy + Math.sin(angle) * r3
          return (
            <line key={i} x1={x1} y1={y1} x2={x2} y2={y2} stroke="currentColor" strokeWidth="0.6" opacity="0.36" />
          )
        })}

        {/* 12 diamonds at petal tips */}
        {Array.from({ length: 12 }).map((_, i) => {
          const angle = (i * 30 * Math.PI) / 180
          const cx = size / 2 + Math.cos(angle) * r3
          const cy = size / 2 + Math.sin(angle) * r3
          return (
            <rect
              key={i}
              x={cx - 2}
              y={cy - 2}
              width="4"
              height="4"
              fill="currentColor"
              opacity="0.36"
              transform={`rotate(45 ${cx} ${cy})`}
            />
          )
        })}

        {/* Outer dotted ring */}
        <circle cx={size / 2} cy={size / 2} r={r4} stroke="currentColor" strokeWidth="0.5" opacity="0.22" strokeDasharray="1 6" />
      </g>
    </svg>
  )
}
