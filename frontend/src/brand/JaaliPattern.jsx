// Jaali — Indian stone screen geometry. Uses repeated octagram (8-point star)
// motifs cut from stone, with thin connecting lines. Reads as architectural
// rhythm, not decoration. SVG pattern lets us tile it seamlessly.

let _idCounter = 0
function uniqueId(prefix) {
  _idCounter += 1
  return `${prefix}-${_idCounter}`
}

// Repeating octagram lattice — for hero edges, large background panels.
export function JaaliLattice({ className = '', size = 56, opacity = 0.5, color = 'currentColor' }) {
  const id = uniqueId('jaali')
  return (
    <svg className={className} width="100%" height="100%" aria-hidden style={{ opacity }}>
      <defs>
        <pattern id={id} x="0" y="0" width={size} height={size} patternUnits="userSpaceOnUse">
          {/* Octagram (8-point star) at the center */}
          <g transform={`translate(${size / 2} ${size / 2})`} fill="none" stroke={color} strokeWidth="0.6">
            <rect x={-size * 0.32} y={-size * 0.32} width={size * 0.64} height={size * 0.64} />
            <rect
              x={-size * 0.32} y={-size * 0.32} width={size * 0.64} height={size * 0.64}
              transform="rotate(45)"
            />
            <circle cx="0" cy="0" r={size * 0.08} />
          </g>
          {/* Corner connectors */}
          <line x1="0" y1={size / 2} x2={size} y2={size / 2} stroke={color} strokeWidth="0.4" opacity="0.4" />
          <line x1={size / 2} y1="0" x2={size / 2} y2={size} stroke={color} strokeWidth="0.4" opacity="0.4" />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// Vertical jaali column — Indian temple-screen style for page edges.
export function JaaliColumn({ className = '', rows = 8, color = 'var(--terracotta)' }) {
  return (
    <div className={className} aria-hidden>
      <svg width="64" height="100%" viewBox={`0 0 64 ${rows * 64}`} preserveAspectRatio="xMidYMid meet">
        {[...Array(rows)].map((_, r) => {
          const cy = r * 64 + 32
          return (
            <g key={r} transform={`translate(32 ${cy})`} fill="none" stroke={color} strokeWidth="0.8" opacity="0.45">
              {/* outer square */}
              <rect x="-22" y="-22" width="44" height="44" />
              {/* inner rotated square (octagram) */}
              <rect x="-22" y="-22" width="44" height="44" transform="rotate(45)" />
              {/* center dot */}
              <circle cx="0" cy="0" r="2.5" fill={color} stroke="none" opacity="0.7" />
              {/* connecting line down to next */}
              {r < rows - 1 && (
                <line x1="0" y1="22" x2="0" y2="42" stroke={color} strokeWidth="0.4" opacity="0.4" />
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

// Diamond / lozenge field — Mughal jali field pattern.
// Used for hero/large-canvas backgrounds. Restful but architectural.
export function JaaliDiamondField({ className = '', size = 64, opacity = 0.08, color = 'currentColor' }) {
  const id = uniqueId('jaali-diamond')
  return (
    <svg className={className} width="100%" height="100%" aria-hidden style={{ opacity }}>
      <defs>
        <pattern id={id} x="0" y="0" width={size} height={size} patternUnits="userSpaceOnUse">
          <g transform={`translate(${size / 2} ${size / 2})`} fill="none" stroke={color} strokeWidth="0.6">
            {/* outer diamond */}
            <polygon points={`0,${-size * 0.42} ${size * 0.42},0 0,${size * 0.42} ${-size * 0.42},0`} />
            {/* inner diamond */}
            <polygon points={`0,${-size * 0.22} ${size * 0.22},0 0,${size * 0.22} ${-size * 0.22},0`} opacity="0.7" />
            {/* center dot */}
            <circle cx="0" cy="0" r={size * 0.05} fill={color} stroke="none" opacity="0.9" />
          </g>
          {/* corner connectors — half-diamonds that complete on tiling */}
          <g fill="none" stroke={color} strokeWidth="0.4" opacity="0.5">
            <line x1="0" y1={size / 2} x2={size * 0.08} y2={size / 2} />
            <line x1={size - size * 0.08} y1={size / 2} x2={size} y2={size / 2} />
            <line x1={size / 2} y1="0" x2={size / 2} y2={size * 0.08} />
            <line x1={size / 2} y1={size - size * 0.08} x2={size / 2} y2={size} />
          </g>
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// Mughal arch row — silhouette of stepped pointed arches as a top divider.
// Reads as a temple-screen elevation. Use as a thin section header band.
export function JaaliArchRow({ className = '', count = 14, height = 48, color = 'var(--terracotta)' }) {
  const w = 72
  const arches = [...Array(count)].map((_, i) => {
    const x = i * w
    return (
      <g key={i} transform={`translate(${x} 0)`} fill="none" stroke={color} strokeWidth="0.8" opacity="0.55">
        {/* outer pointed arch silhouette */}
        <path d={`M 4 ${height} L 4 ${height * 0.45} Q 4 ${height * 0.18} ${w / 2} ${height * 0.06} Q ${w - 4} ${height * 0.18} ${w - 4} ${height * 0.45} L ${w - 4} ${height}`} />
        {/* inner echo */}
        <path d={`M 12 ${height} L 12 ${height * 0.5} Q 12 ${height * 0.28} ${w / 2} ${height * 0.18} Q ${w - 12} ${height * 0.28} ${w - 12} ${height * 0.5} L ${w - 12} ${height}`} opacity="0.55" />
        {/* keystone diamond */}
        <polygon points={`${w / 2},${height * 0.55} ${w / 2 + 3},${height * 0.62} ${w / 2},${height * 0.69} ${w / 2 - 3},${height * 0.62}`} fill={color} stroke="none" opacity="0.7" />
        {/* base ground line */}
        <line x1="0" y1={height} x2={w} y2={height} strokeWidth="0.5" opacity="0.4" />
      </g>
    )
  })
  return (
    <svg className={className} width="100%" height={height} viewBox={`0 0 ${count * w} ${height}`} preserveAspectRatio="xMidYMax slice" aria-hidden>
      {arches}
    </svg>
  )
}

// Concentric rangoli — paired concentric circles with cross spokes.
// Used as a single ornamental anchor (centered) above a section heading.
export function JaaliRosette({ className = '', size = 96, color = 'var(--terracotta)' }) {
  const r1 = size * 0.46, r2 = size * 0.34, r3 = size * 0.22, r4 = size * 0.10
  return (
    <svg className={className} width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden>
      <g transform={`translate(${size / 2} ${size / 2})`} fill="none" stroke={color} strokeWidth="0.8" opacity="0.85">
        <circle r={r1} opacity="0.35" />
        <circle r={r2} opacity="0.55" />
        <circle r={r3} opacity="0.75" />
        <circle r={r4} fill={color} stroke="none" opacity="0.9" />
        {/* 8 cardinal spokes that don't cross the inner circle */}
        {[0, 45, 90, 135, 180, 225, 270, 315].map(deg => (
          <line
            key={deg}
            x1={Math.cos((deg * Math.PI) / 180) * (r4 + 2)}
            y1={Math.sin((deg * Math.PI) / 180) * (r4 + 2)}
            x2={Math.cos((deg * Math.PI) / 180) * r1}
            y2={Math.sin((deg * Math.PI) / 180) * r1}
            opacity={deg % 90 === 0 ? 0.7 : 0.35}
          />
        ))}
        {/* corner diamonds */}
        {[0, 90, 180, 270].map(deg => {
          const cx = Math.cos((deg * Math.PI) / 180) * (r1 + 4)
          const cy = Math.sin((deg * Math.PI) / 180) * (r1 + 4)
          return <polygon key={deg} points={`${cx},${cy - 3} ${cx + 3},${cy} ${cx},${cy + 3} ${cx - 3},${cy}`} fill={color} stroke="none" opacity="0.7" />
        })}
      </g>
    </svg>
  )
}

// Woven linework — interlaced lines like the brass screen behind a temple lamp.
// Diagonal, dense, tileable. Use behind dense / dark / accent-coloured sections.
export function JaaliWeave({ className = '', size = 32, opacity = 0.08, color = 'currentColor' }) {
  const id = uniqueId('jaali-weave')
  return (
    <svg className={className} width="100%" height="100%" aria-hidden style={{ opacity }}>
      <defs>
        <pattern id={id} x="0" y="0" width={size} height={size} patternUnits="userSpaceOnUse">
          <g fill="none" stroke={color} strokeWidth="0.6">
            <line x1="0" y1="0" x2={size} y2={size} opacity="0.7" />
            <line x1="0" y1={size} x2={size} y2="0" opacity="0.4" />
            <line x1={size / 2} y1="0" x2={size / 2} y2={size} opacity="0.18" />
            <line x1="0" y1={size / 2} x2={size} y2={size / 2} opacity="0.18" />
            <circle cx={size / 2} cy={size / 2} r="1.2" fill={color} stroke="none" opacity="0.7" />
          </g>
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  )
}

// Horizontal connecting band — paired arcs / rangoli rhythm.
export function JaaliBand({ className = '', count = 6, color = 'var(--copper)' }) {
  return (
    <svg className={className} width="100%" height="60" viewBox="0 0 600 60" preserveAspectRatio="xMidYMid meet" aria-hidden>
      {[...Array(count)].map((_, i) => {
        const cx = (i + 0.5) * (600 / count)
        return (
          <g key={i} transform={`translate(${cx} 30)`} fill="none" stroke={color} strokeWidth="0.7" opacity="0.4">
            <circle cx="0" cy="0" r="14" />
            <circle cx="0" cy="0" r="22" opacity="0.5" />
            <line x1="-30" y1="0" x2="-22" y2="0" />
            <line x1="22" y1="0" x2="30" y2="0" />
          </g>
        )
      })}
      {/* baseline */}
      <line x1="0" y1="30" x2="600" y2="30" stroke={color} strokeWidth="0.4" opacity="0.18" />
    </svg>
  )
}
