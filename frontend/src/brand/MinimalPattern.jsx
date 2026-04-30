import './MinimalPattern.css'

// Family of low-opacity, hairline SVG backgrounds with Indian-modern
// geometric character. Always absolutely-positioned, decorative only.
// Color comes from currentColor.

// 1. DotGrid — faint dotted lattice.
export function DotGrid({ className = '', spacing = 28, dot = 1.2 }) {
  const id = `dots-${spacing}`
  return (
    <svg className={`mp ${className}`.trim()} width="100%" height="100%" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <pattern id={id} x="0" y="0" width={spacing} height={spacing} patternUnits="userSpaceOnUse">
          <circle cx={spacing / 2} cy={spacing / 2} r={dot / 2} fill="currentColor" opacity="0.55" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// 2. JaaliBand — horizontal hairline jaali strip with diamond tessellation.
export function JaaliBand({ className = '' }) {
  return (
    <svg className={`mp mp--band ${className}`.trim()} viewBox="0 0 1200 32" preserveAspectRatio="none" aria-hidden="true">
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

// 3. CornerRangoli — concentric arcs + radial petal lines anchored to a corner.
export function CornerRangoli({ className = '', corner = 'tr', size = 360 }) {
  const arcs = [size * 0.26, size * 0.4, size * 0.56, size * 0.72]
  return (
    <svg className={`mp mp--rangoli mp--rangoli-${corner} ${className}`.trim()}
         width={size} height={size} viewBox={`0 0 ${size} ${size}`} fill="none" aria-hidden="true">
      <g stroke="currentColor" fill="none" opacity="0.4">
        {arcs.map((r, i) => (
          <circle key={i} cx={size} cy="0" r={r}
            strokeWidth={i === 1 ? 0.7 : 0.45}
            strokeDasharray={i % 2 === 0 ? '1 5' : 'none'}
            opacity={0.5 - i * 0.08} />
        ))}
        {Array.from({ length: 8 }).map((_, i) => {
          const angle = (90 + i * 11.25) * Math.PI / 180
          return (
            <line key={i} x1={size} y1="0"
              x2={size + Math.cos(angle) * size * 0.78}
              y2={Math.sin(angle) * size * 0.78}
              strokeWidth="0.4" opacity="0.32" />
          )
        })}
      </g>
    </svg>
  )
}

// 4. StepwellLines — concentric stepped squares (baoli motif).
export function StepwellLines({ className = '', size = 260 }) {
  const steps = 6
  return (
    <svg className={`mp mp--stepwell ${className}`.trim()}
         width={size} height={size} viewBox={`0 0 ${size} ${size}`} fill="none" aria-hidden="true">
      <g stroke="currentColor" fill="none" opacity="0.42">
        {Array.from({ length: steps }).map((_, i) => {
          const inset = (i + 1) * (size / (steps * 2))
          return (
            <rect key={i} x={inset} y={inset}
              width={size - inset * 2} height={size - inset * 2}
              strokeWidth="0.55" opacity={0.55 - i * 0.07} />
          )
        })}
        <rect x={size / 2 - 3} y={size / 2 - 3} width="6" height="6"
          stroke="none" fill="currentColor" opacity="0.55"
          transform={`rotate(45 ${size / 2} ${size / 2})`} />
      </g>
    </svg>
  )
}

// 5. OctagramTile — repeating 8-point star tessellation. Use as a tiled
//    field for hero washes or large empty space.
export function OctagramTile({ className = '', spacing = 64 }) {
  const id = `octa-${spacing}`
  const c = spacing / 2
  const r = spacing * 0.36
  return (
    <svg className={`mp ${className}`.trim()} width="100%" height="100%"
         preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <pattern id={id} x="0" y="0" width={spacing} height={spacing} patternUnits="userSpaceOnUse">
          <g stroke="currentColor" strokeWidth="0.5" fill="none" opacity="0.5">
            <rect x={c - r} y={c - r} width={r * 2} height={r * 2} />
            <rect x={c - r} y={c - r} width={r * 2} height={r * 2}
              transform={`rotate(45 ${c} ${c})`} />
          </g>
          <circle cx={c} cy={c} r="0.9" fill="currentColor" opacity="0.45" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// 6. ChakraWheel — 16-spoke radial wheel (sun chakra). Rotates very slowly.
export function ChakraWheel({ className = '', size = 320, spokes = 16, animate = true }) {
  const c = size / 2
  return (
    <svg className={`mp mp--chakra ${animate ? 'mp--anim' : ''} ${className}`.trim()}
         width={size} height={size} viewBox={`0 0 ${size} ${size}`} fill="none" aria-hidden="true">
      <g stroke="currentColor" fill="none" opacity="0.42">
        <circle cx={c} cy={c} r={size * 0.42} strokeWidth="0.6" />
        <circle cx={c} cy={c} r={size * 0.32} strokeWidth="0.45" strokeDasharray="2 6" />
        <circle cx={c} cy={c} r={size * 0.18} strokeWidth="0.5" />
        <g className="mp__chakra-spokes">
          {Array.from({ length: spokes }).map((_, i) => {
            const a = (i * 360 / spokes) * Math.PI / 180
            return (
              <line key={i}
                x1={c + Math.cos(a) * size * 0.18}
                y1={c + Math.sin(a) * size * 0.18}
                x2={c + Math.cos(a) * size * 0.42}
                y2={c + Math.sin(a) * size * 0.42}
                strokeWidth="0.5" opacity="0.5" />
            )
          })}
        </g>
        {/* tiny diamonds at each spoke tip */}
        {Array.from({ length: spokes }).map((_, i) => {
          const a = (i * 360 / spokes) * Math.PI / 180
          const x = c + Math.cos(a) * size * 0.42
          const y = c + Math.sin(a) * size * 0.42
          return (
            <rect key={i} x={x - 1.2} y={y - 1.2} width="2.4" height="2.4"
              stroke="none" fill="currentColor" opacity="0.5"
              transform={`rotate(45 ${x} ${y})`} />
          )
        })}
      </g>
    </svg>
  )
}

// 7. HexLattice — repeating hexagon lattice, restrained. Engineering-feel.
export function HexLattice({ className = '', size = 28 }) {
  const id = `hex-${size}`
  const w = size
  const h = size * Math.sqrt(3)
  return (
    <svg className={`mp ${className}`.trim()} width="100%" height="100%"
         preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <pattern id={id} x="0" y="0" width={w * 1.5} height={h} patternUnits="userSpaceOnUse">
          <g stroke="currentColor" strokeWidth="0.45" fill="none" opacity="0.35">
            <path d={`M ${w * 0.25} 0 L ${w * 0.75} 0 L ${w} ${h * 0.5} L ${w * 0.75} ${h} L ${w * 0.25} ${h} L 0 ${h * 0.5} Z`} />
            <path d={`M ${w} ${h * 0.5} L ${w * 1.25} ${h * 0} L ${w * 1.5} ${h * 0.5} L ${w * 1.25} ${h}`} opacity="0.7" />
          </g>
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// 8. CrossHairCorner — fine plus-mark grid scattered in a corner. Surveyor /
//    drafting-paper cue. Quiet, technical.
export function CrossHairCorner({ className = '', size = 220, spacing = 22 }) {
  const dots = []
  const cols = Math.floor(size / spacing)
  for (let r = 0; r < cols; r++) {
    for (let c = 0; c < cols; c++) {
      const x = c * spacing + spacing / 2
      const y = r * spacing + spacing / 2
      const opacity = 0.5 - (r + c) * 0.04
      if (opacity <= 0) continue
      dots.push(
        <g key={`${r}-${c}`} stroke="currentColor" strokeWidth="0.45" opacity={opacity}>
          <line x1={x - 2} y1={y} x2={x + 2} y2={y} />
          <line x1={x} y1={y - 2} x2={x} y2={y + 2} />
        </g>
      )
    }
  }
  return (
    <svg className={`mp ${className}`.trim()} width={size} height={size}
         viewBox={`0 0 ${size} ${size}`} fill="none" aria-hidden="true">
      {dots}
    </svg>
  )
}
