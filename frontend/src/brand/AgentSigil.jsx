import { getSigilTraits } from './sigilTraits'

function djb2(str) {
  let h = 5381
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) + h) ^ str.charCodeAt(i)
    h = h | 0
  }
  return Math.abs(h)
}

function mulberry32(seed) {
  return function () {
    seed |= 0; seed = seed + 0x6D2B79F5 | 0
    let t = Math.imul(seed ^ seed >>> 15, 1 | seed)
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t
    return ((t ^ t >>> 14) >>> 0) / 4294967296
  }
}

// Recursive Mondrian-style rect subdivision
function splitRect(x, y, w, h, depth, rand) {
  const minDim = 6
  if (depth === 0 || (w < minDim && h < minDim)) return [{ x, y, w, h }]
  if (rand() < 0.22) return [{ x, y, w, h }]  // sometimes don't split

  const horiz = w >= h ? rand() < 0.42 : rand() < 0.62
  const t = 0.3 + rand() * 0.4

  if (horiz) {
    const split = h * t
    return [
      ...splitRect(x, y,          w, split,       depth - 1, rand),
      ...splitRect(x, y + split,  w, h - split,   depth - 1, rand),
    ]
  } else {
    const split = w * t
    return [
      ...splitRect(x,          y, split,       h, depth - 1, rand),
      ...splitRect(x + split,  y, w - split,   h, depth - 1, rand),
    ]
  }
}

// Build a 5-color Mondrian palette from a base hue
function buildPalette(baseHue) {
  const h2 = (baseHue + 160) % 360
  const h3 = (baseHue +  45) % 360
  return [
    `hsl(${baseHue}, 68%, 44%)`,   // deep primary
    `hsl(${baseHue}, 55%, 62%)`,   // lighter primary
    `hsl(${h2},       62%, 50%)`,  // complementary
    `hsl(${h3},       72%, 58%)`,  // warm accent
    `hsl(${baseHue},  18%, 24%)`,  // near-black tint
  ]
}

const SIZES = { xs: 20, sm: 32, md: 52, lg: 96, xl: 128 }
const RADII = { xs: 4,  sm: 7,  md: 10, lg: 14, xl: 18  }
const DEPTH = { xs: 0,  sm: 1,  md: 3,  lg: 4,  xl: 4   }

export default function AgentSigil({ agentId, size = 'md', className, style }) {
  const px    = SIZES[size] ?? SIZES.md
  const rx    = RADII[size] ?? RADII.md
  const depth = DEPTH[size] ?? DEPTH.md

  const seed   = djb2(String(agentId ?? 'default'))
  const rand   = mulberry32(seed)
  const traits = getSigilTraits(agentId ?? 'default')

  const baseHue = Math.floor(rand() * 360)
  const palette = buildPalette(baseHue)

  const clipId = `mc-${seed}-${size}`

  // xs and sm: just base color + 1 accent circle (no subdivision)
  if (size === 'xs' || size === 'sm') {
    const bg  = palette[0]
    const acc = palette[2]
    const cx  = px * (0.4 + rand() * 0.3)
    const cy  = px * (0.3 + rand() * 0.35)
    const r   = px * (0.22 + rand() * 0.12)
    return (
      <svg width={px} height={px} viewBox={`0 0 ${px} ${px}`} aria-hidden="true"
        className={className} style={{ display: 'block', flexShrink: 0, ...style }}>
        <rect width={px} height={px} rx={rx} fill={bg} />
        <circle cx={cx} cy={cy} r={r} fill={acc} opacity="0.85" />
      </svg>
    )
  }

  // md, lg, xl: Mondrian subdivision
  const rects = splitRect(0, 0, px, px, depth, rand)

  // Assign colors - avoid placing same color adjacent (simple modulo rule)
  const coloredRects = rects.map((r, i) => ({
    ...r,
    fill: palette[i % palette.length],
  }))

  // Add 1–2 small dot accents on top for visual interest at larger sizes
  const dots = size !== 'md' ? [] : [
    { cx: rand() * px, cy: rand() * px, r: px * 0.05 + rand() * px * 0.04, fill: 'rgba(255,255,255,0.55)' },
  ]

  return (
    <svg
      width={px}
      height={px}
      viewBox={`0 0 ${px} ${px}`}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      style={{ display: 'block', flexShrink: 0, borderRadius: rx, ...style }}
      aria-hidden="true"
    >
      <defs>
        <clipPath id={clipId}>
          <rect x="0" y="0" width={px} height={px} rx={rx} />
        </clipPath>
      </defs>

      <g clipPath={`url(#${clipId})`}>
        {/* Mondrian rects */}
        {coloredRects.map((r, i) => (
          <rect
            key={i}
            x={r.x} y={r.y} width={r.w} height={r.h}
            fill={r.fill}
          />
        ))}

        {/* Thin separating lines between rects for the Mondrian grid effect */}
        {coloredRects.map((r, i) => (
          <rect
            key={`border-${i}`}
            x={r.x} y={r.y} width={r.w} height={r.h}
            fill="none"
            stroke="rgba(0,0,0,0.12)"
            strokeWidth="0.8"
          />
        ))}

        {/* Dot accents */}
        {dots.map((d, i) => (
          <circle key={`dot-${i}`} cx={d.cx} cy={d.cy} r={d.r} fill={d.fill} />
        ))}
      </g>

      {/* Outer border ring */}
      <rect x="0" y="0" width={px} height={px} rx={rx}
        fill="none" stroke="rgba(0,0,0,0.08)" strokeWidth="1" />
    </svg>
  )
}
