// AgentSigil — deterministic Indian-motif thumbnail for every agent.
// Hashes agent_id → picks one of 6 motif families (Jaali, Stepwell, Chakra,
// Rangoli, Kolam, Paisley), then a second hash drives variant + palette.
// All families are sharp/geometric: straight edges, mitered corners, no soft
// curves except the outer rounded clip. Same agent_id → same sigil.

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

// Indian-tilted palette: terracotta, indigo, ochre, peacock teal, vermillion,
// sandstone, copper, near-black. Each agent gets a deterministic 4-colour set
// — base, contrast, accent, surface — picked from this pool.
const INDIAN_COLOURS = [
  '#b85c3a', // terracotta
  '#3f4d8a', // indigo
  '#d3a24a', // ochre
  '#1f6e6c', // peacock teal
  '#c2342a', // vermillion
  '#7a4f30', // copper
  '#4a6b2a', // olive
  '#8a3a5a', // wine
]
const SURFACES = ['#f4ead8', '#ecdcc0', '#e6cfa3', '#d8c099'] // sandstone family

function pickPalette(rand) {
  // base + contrast must read against each other; accent is a brighter pop;
  // surface is the warm background.
  const idx = Math.floor(rand() * INDIAN_COLOURS.length)
  const base = INDIAN_COLOURS[idx]
  // step 3 around the wheel for contrast
  const contrast = INDIAN_COLOURS[(idx + 3) % INDIAN_COLOURS.length]
  const accent = INDIAN_COLOURS[(idx + 5) % INDIAN_COLOURS.length]
  const surface = SURFACES[Math.floor(rand() * SURFACES.length)]
  return [base, contrast, accent, surface]
}

// Weighted family selector — sharp/geometric tilt: Jaali, Stepwell, Chakra
// come up roughly twice as often as Rangoli, Kolam, Paisley.
const FAMILY_WEIGHTS = [
  ['jaali',    2],
  ['stepwell', 2],
  ['chakra',   2],
  ['rangoli',  1],
  ['kolam',    1],
  ['paisley',  1],
]
const TOTAL_WEIGHT = FAMILY_WEIGHTS.reduce((s, [, w]) => s + w, 0)

function pickFamily(rand) {
  let r = rand() * TOTAL_WEIGHT
  for (const [name, w] of FAMILY_WEIGHTS) {
    if ((r -= w) < 0) return name
  }
  return FAMILY_WEIGHTS[0][0]
}

// ── Family renderers ───────────────────────────────────────────────────────
// Each returns an array of SVG children. Background rect is drawn by the
// caller so the surface colour stays consistent across families.

function renderJaali(rand, px, palette) {
  const [base, contrast, accent] = palette
  const tiles = rand() < 0.5 ? 2 : 3
  const cell = px / tiles
  const out = []
  for (let r = 0; r < tiles; r++) {
    for (let c = 0; c < tiles; c++) {
      const cx = c * cell + cell / 2
      const cy = r * cell + cell / 2
      const half = cell * 0.38
      out.push(
        <rect key={`j-a-${r}-${c}`} x={cx - half} y={cy - half}
          width={half * 2} height={half * 2} fill={base} />
      )
      out.push(
        <rect key={`j-b-${r}-${c}`} x={cx - half} y={cy - half}
          width={half * 2} height={half * 2} fill={contrast} fillOpacity={0.78}
          transform={`rotate(45 ${cx} ${cy})`} />
      )
      if (rand() < 0.55) {
        out.push(
          <rect key={`j-c-${r}-${c}`}
            x={cx - cell * 0.08} y={cy - cell * 0.08}
            width={cell * 0.16} height={cell * 0.16}
            fill={accent} transform={`rotate(45 ${cx} ${cy})`} />
        )
      }
    }
  }
  return out
}

function renderStepwell(rand, px, palette) {
  const [base, contrast, accent] = palette
  const steps = 3 + Math.floor(rand() * 3) // 3..5
  const out = []
  for (let i = 0; i < steps; i++) {
    const t = i / steps
    const inset = t * px * 0.42
    const size = px - inset * 2
    out.push(
      <rect key={`sw-${i}`} x={inset} y={inset}
        width={size} height={size}
        fill={i % 2 === 0 ? base : contrast} />
    )
  }
  // central square accent
  const cs = px * 0.10
  out.push(
    <rect key="sw-c" x={px / 2 - cs / 2} y={px / 2 - cs / 2}
      width={cs} height={cs} fill={accent} />
  )
  // optional 45° rotation for half the agents — different silhouette
  if (rand() < 0.45) {
    return [
      <g key="sw-rot" transform={`rotate(45 ${px / 2} ${px / 2})`}>{out}</g>,
    ]
  }
  return out
}

function renderChakra(rand, px, palette) {
  const [base, contrast, accent] = palette
  const spokes = [8, 12, 16, 24][Math.floor(rand() * 4)]
  const out = []
  const cx = px / 2, cy = px / 2
  // outer rim
  out.push(
    <circle key="c-rim" cx={cx} cy={cy} r={px * 0.42}
      fill="none" stroke={base} strokeWidth={px * 0.05} />
  )
  // spokes
  const inner = px * 0.10
  const outer = px * 0.40
  for (let i = 0; i < spokes; i++) {
    const a = (i / spokes) * Math.PI * 2
    const x1 = cx + Math.cos(a) * inner
    const y1 = cy + Math.sin(a) * inner
    const x2 = cx + Math.cos(a) * outer
    const y2 = cy + Math.sin(a) * outer
    out.push(
      <line key={`c-s-${i}`} x1={x1} y1={y1} x2={x2} y2={y2}
        stroke={contrast} strokeWidth={px * 0.022} strokeLinecap="square" />
    )
  }
  // hub
  out.push(<circle key="c-h1" cx={cx} cy={cy} r={px * 0.13} fill={base} />)
  out.push(<circle key="c-h2" cx={cx} cy={cy} r={px * 0.06} fill={accent} />)
  return out
}

function renderRangoli(rand, px, palette) {
  const [base, contrast, accent] = palette
  const fold = [6, 8, 12][Math.floor(rand() * 3)]
  const out = []
  const cx = px / 2, cy = px / 2
  // outer ring of pointed triangles
  const outer = px * 0.45
  const inner = px * 0.26
  const half = (Math.PI / fold) * 0.62
  for (let i = 0; i < fold; i++) {
    const a = (i / fold) * Math.PI * 2 - Math.PI / 2
    const tipX = cx + Math.cos(a) * outer
    const tipY = cy + Math.sin(a) * outer
    const b1x = cx + Math.cos(a - half) * inner
    const b1y = cy + Math.sin(a - half) * inner
    const b2x = cx + Math.cos(a + half) * inner
    const b2y = cy + Math.sin(a + half) * inner
    out.push(
      <polygon key={`r-t-${i}`}
        points={`${tipX},${tipY} ${b1x},${b1y} ${b2x},${b2y}`}
        fill={i % 2 === 0 ? base : contrast} />
    )
  }
  // hexagram core
  const coreR = px * 0.16
  const tri = (rot) => {
    const pts = []
    for (let i = 0; i < 3; i++) {
      const a = (i / 3) * Math.PI * 2 + rot
      pts.push(`${cx + Math.cos(a) * coreR},${cy + Math.sin(a) * coreR}`)
    }
    return pts.join(' ')
  }
  out.push(
    <polygon key="r-c1" points={tri(-Math.PI / 2)} fill={accent} />,
    <polygon key="r-c2" points={tri(Math.PI / 2)}
      fill={accent} fillOpacity={0.55} />
  )
  return out
}

function renderKolam(rand, px, palette) {
  const [base, contrast, accent] = palette
  const grid = [3, 4, 5][Math.floor(rand() * 3)]
  const stroke = px * 0.04
  const inset = px * 0.12
  const cx = px / 2, cy = px / 2
  const out = []
  // outer square loop
  out.push(
    <rect key="k-out" x={inset} y={inset}
      width={px - inset * 2} height={px - inset * 2}
      fill="none" stroke={base} strokeWidth={stroke} />
  )
  // inner rotated diamond
  const r2 = (px - inset * 2) * 0.42
  const diamondPts = [
    `${cx},${cy - r2}`, `${cx + r2},${cy}`,
    `${cx},${cy + r2}`, `${cx - r2},${cy}`,
  ].join(' ')
  out.push(
    <polygon key="k-dia" points={diamondPts}
      fill="none" stroke={contrast} strokeWidth={stroke} />
  )
  // dot grid
  const cellInset = px * 0.22
  const span = px - cellInset * 2
  const step = span / (grid - 1)
  const dotR = px * 0.03
  for (let r = 0; r < grid; r++) {
    for (let c = 0; c < grid; c++) {
      out.push(
        <circle key={`k-d-${r}-${c}`}
          cx={cellInset + c * step} cy={cellInset + r * step}
          r={dotR} fill={accent} />
      )
    }
  }
  return out
}

function renderPaisley(rand, px, palette) {
  const [base, contrast, accent] = palette
  const cx = px / 2, cy = px / 2
  const r = px * 0.4
  // chamfered teardrop curl, all straight segments
  const pts = [
    [cx + r * 0.55, cy - r * 0.85],   // top tip
    [cx + r * 0.20, cy - r * 0.50],
    [cx + r * 0.55, cy - r * 0.05],   // mid right
    [cx + r * 0.45, cy + r * 0.45],   // bottom right
    [cx + r * 0.05, cy + r * 0.70],   // bottom
    [cx - r * 0.45, cy + r * 0.45],   // bottom left
    [cx - r * 0.65, cy - r * 0.05],   // mid left
    [cx - r * 0.40, cy - r * 0.55],   // upper left
    [cx - r * 0.05, cy - r * 0.65],   // top centre
  ].map(p => p.join(',')).join(' ')

  const out = [
    <polygon key="p-body" points={pts} fill={base} />,
  ]
  const mirrored = rand() < 0.4
  if (mirrored) {
    out.push(
      <polygon key="p-mirror" points={pts}
        fill={contrast} fillOpacity={0.55}
        transform={`scale(-1 1) translate(${-px} 0)`} />
    )
  }
  // chamfered inner cutout for depth
  const inner = pts.split(' ').map(s => {
    const [x, y] = s.split(',').map(Number)
    return `${cx + (x - cx) * 0.5},${cy + (y - cy) * 0.5}`
  }).join(' ')
  out.push(
    <polygon key="p-inner" points={inner}
      fill={accent} fillOpacity={0.6} />
  )
  return out
}

const RENDERERS = {
  jaali: renderJaali,
  stepwell: renderStepwell,
  chakra: renderChakra,
  rangoli: renderRangoli,
  kolam: renderKolam,
  paisley: renderPaisley,
}

const SIZES = { xs: 20, sm: 32, md: 52, lg: 96, xl: 128 }
const RADII = { xs: 4,  sm: 7,  md: 10, lg: 14, xl: 18  }

export default function AgentSigil({ agentId, size = 'md', className, style }) {
  const px = SIZES[size] ?? SIZES.md
  const rx = RADII[size] ?? RADII.md

  const seed = djb2(String(agentId ?? 'default'))
  const rand = mulberry32(seed)

  // Palette is picked first so xs/sm avatars match larger sigils
  const palette = pickPalette(rand)
  const [base, , accent, surface] = palette
  const clipId = `as-${seed}-${size}`

  // Tiny sizes: just surface + accent dot — no motif room
  if (size === 'xs' || size === 'sm') {
    const cx = px * (0.4 + rand() * 0.3)
    const cy = px * (0.3 + rand() * 0.35)
    const r  = px * (0.22 + rand() * 0.12)
    return (
      <svg width={px} height={px} viewBox={`0 0 ${px} ${px}`} aria-hidden="true"
        className={className}
        style={{ display: 'block', flexShrink: 0, borderRadius: rx, ...style }}>
        <rect width={px} height={px} rx={rx} fill={base} />
        <circle cx={cx} cy={cy} r={r} fill={accent} opacity="0.85" />
      </svg>
    )
  }

  const family = pickFamily(rand)
  const children = RENDERERS[family](rand, px, palette)

  return (
    <svg
      width={px} height={px} viewBox={`0 0 ${px} ${px}`}
      fill="none" xmlns="http://www.w3.org/2000/svg"
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
        <rect width={px} height={px} style={{ fill: 'var(--surface-2, ' + surface + ')' }} />
        {children}
      </g>
      <rect x="0" y="0" width={px} height={px} rx={rx}
        fill="none" stroke="rgba(0,0,0,0.10)" strokeWidth="1" />
    </svg>
  )
}
