import { getSigilTraits } from './sigilTraits'

const SIZES = { xs: 20, sm: 32, md: 64, lg: 128, xl: 280 }

function OrbitShape({ colors, gradId, strokeW, r = 38 }) {
  return (
    <>
      <circle cx="50" cy="50" r={r} fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW} />
      <ellipse cx="50" cy="50" rx={r} ry={r * 0.35} fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW * 0.6} opacity="0.6" />
      <circle cx={50 + r} cy="50" r={strokeW * 1.8} fill={colors[0]} />
    </>
  )
}

function HexShape({ colors, gradId, strokeW }) {
  const pts = Array.from({ length: 6 }, (_, i) => {
    const a = (Math.PI / 3) * i - Math.PI / 6
    return `${50 + 36 * Math.cos(a)},${50 + 36 * Math.sin(a)}`
  }).join(' ')
  const pts2 = Array.from({ length: 6 }, (_, i) => {
    const a = (Math.PI / 3) * i - Math.PI / 6
    return `${50 + 20 * Math.cos(a)},${50 + 20 * Math.sin(a)}`
  }).join(' ')
  return (
    <>
      <polygon points={pts} fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW} />
      <polygon points={pts2} fill={`url(#${gradId})`} opacity="0.15" />
    </>
  )
}

function PrismShape({ colors, gradId, strokeW }) {
  return (
    <>
      <polygon points="50,12 85,72 15,72" fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW} />
      <polygon points="50,28 70,62 30,62" fill={`url(#${gradId})`} opacity="0.15" />
      <line x1="50" y1="12" x2="50" y2="72" stroke={`url(#${gradId})`} strokeWidth={strokeW * 0.5} opacity="0.5" />
    </>
  )
}

function MeshShape({ colors, gradId, strokeW }) {
  const pts = Array.from({ length: 5 }, (_, i) => {
    const a = (Math.PI * 2 / 5) * i - Math.PI / 2
    return [50 + 34 * Math.cos(a), 50 + 34 * Math.sin(a)]
  })
  const lines = []
  for (let i = 0; i < pts.length; i++)
    for (let j = i + 1; j < pts.length; j++)
      lines.push(<line key={`${i}${j}`} x1={pts[i][0]} y1={pts[i][1]} x2={pts[j][0]} y2={pts[j][1]} stroke={`url(#${gradId})`} strokeWidth={strokeW * 0.6} opacity="0.6" />)
  return (
    <>
      {lines}
      {pts.map(([x, y], i) => <circle key={i} cx={x} cy={y} r={strokeW * 1.5} fill={colors[0]} />)}
    </>
  )
}

function SpiralShape({ colors, gradId, strokeW }) {
  const path = Array.from({ length: 120 }, (_, i) => {
    const t = (i / 119) * Math.PI * 4
    const r = 5 + t * 3.5
    const x = 50 + r * Math.cos(t)
    const y = 50 + r * Math.sin(t)
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`
  }).join(' ')
  return <path d={path} fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW} strokeLinecap="round" />
}

function RingShape({ colors, gradId, strokeW }) {
  return (
    <>
      <circle cx="50" cy="50" r="36" fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW * 2} strokeDasharray="6 4" />
      <circle cx="50" cy="50" r="22" fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW} opacity="0.5" />
      <circle cx="50" cy="50" r="6" fill={colors[0]} />
    </>
  )
}

function DiamondShape({ colors, gradId, strokeW }) {
  return (
    <>
      <polygon points="50,14 82,50 50,86 18,50" fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW} />
      <polygon points="50,28 68,50 50,72 32,50" fill={`url(#${gradId})`} opacity="0.18" />
      <line x1="18" y1="50" x2="82" y2="50" stroke={`url(#${gradId})`} strokeWidth={strokeW * 0.4} opacity="0.4" />
      <line x1="50" y1="14" x2="50" y2="86" stroke={`url(#${gradId})`} strokeWidth={strokeW * 0.4} opacity="0.4" />
    </>
  )
}

function CrossShape({ colors, gradId, strokeW }) {
  return (
    <>
      <line x1="50" y1="16" x2="50" y2="84" stroke={`url(#${gradId})`} strokeWidth={strokeW * 2} strokeLinecap="round" />
      <line x1="16" y1="50" x2="84" y2="50" stroke={`url(#${gradId})`} strokeWidth={strokeW * 2} strokeLinecap="round" />
      <circle cx="50" cy="50" r="36" fill="none" stroke={`url(#${gradId})`} strokeWidth={strokeW * 0.5} opacity="0.4" />
    </>
  )
}

const SHAPE_MAP = { orbit: OrbitShape, hex: HexShape, prism: PrismShape, mesh: MeshShape, spiral: SpiralShape, ring: RingShape, diamond: DiamondShape, cross: CrossShape }

export default function AgentSigil({ agentId, size = 'md', state = 'idle', className, style }) {
  const { shape, colors, rotation, strokeW, gradId } = getSigilTraits(agentId ?? 'default')
  const px = SIZES[size] ?? SIZES.md
  const ShapeComp = SHAPE_MAP[shape] ?? OrbitShape

  const animStyle = {
    idle:   {},
    active: { animation: 'spin 8s linear infinite' },
    alert:  { animation: 'pulse 1.5s ease-in-out infinite' },
  }[state] ?? {}

  return (
    <svg
      width={px}
      height={px}
      viewBox="0 0 100 100"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      style={{ flexShrink: 0, ...animStyle, ...style }}
    >
      <defs>
        <linearGradient id={gradId} x1="0%" y1="0%" x2="100%" y2="100%">
          <stop offset="0%" stopColor={colors[0]} />
          <stop offset="100%" stopColor={colors[1]} />
        </linearGradient>
      </defs>
      <g transform={`rotate(${rotation} 50 50)`}>
        <ShapeComp colors={colors} gradId={gradId} strokeW={strokeW} />
      </g>
    </svg>
  )
}
