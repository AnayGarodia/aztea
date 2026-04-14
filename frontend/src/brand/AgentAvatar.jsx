import { useEffect, useMemo, useRef } from 'react'
import { gsap } from 'gsap'
import './AgentAvatar.css'

const PALETTES = [
  { shell: '#ff8a47', shell2: '#ff5c7a', eye: '#1d1429', accent: '#ffd166' },
  { shell: '#55d6ff', shell2: '#6a8dff', eye: '#0f1a2f', accent: '#7dffda' },
  { shell: '#78f09b', shell2: '#22c69b', eye: '#0f2120', accent: '#ffe78a' },
  { shell: '#b57dff', shell2: '#7866ff', eye: '#1a1236', accent: '#7ee6ff' },
  { shell: '#ff6cae', shell2: '#ff8a5b', eye: '#2b1130', accent: '#8dfff3' },
]

function hashText(input) {
  let h = 2166136261
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

function mulberry32(seed) {
  let t = seed
  return () => {
    t += 0x6d2b79f5
    let v = Math.imul(t ^ (t >>> 15), 1 | t)
    v ^= v + Math.imul(v ^ (v >>> 7), 61 | v)
    return ((v ^ (v >>> 14)) >>> 0) / 4294967296
  }
}

function createFace(name = 'agent') {
  const rand = mulberry32(hashText(name.toLowerCase()))
  const palette = PALETTES[Math.floor(rand() * PALETTES.length)]
  const eyeKind = Math.floor(rand() * 4)
  const mouthKind = Math.floor(rand() * 4)
  const accessory = Math.floor(rand() * 5)
  const corner = Math.floor(16 + rand() * 18)
  const blink = 2.6 + rand() * 2.4
  const faceScale = 0.94 + rand() * 0.1
  const cheek = rand() > 0.5
  const id = hashText(name).toString(36)
  return { palette, eyeKind, mouthKind, accessory, corner, blink, faceScale, cheek, id }
}

function Eyes({ kind, color }) {
  if (kind === 1) {
    return (
      <>
        <rect x="33" y="42" width="12" height="8" rx="4" fill={color} />
        <rect x="55" y="42" width="12" height="8" rx="4" fill={color} />
      </>
    )
  }

  if (kind === 2) {
    return (
      <>
        <path d="M31 46 L45 44" stroke={color} strokeWidth="3.2" strokeLinecap="round" />
        <path d="M57 44 L69 46" stroke={color} strokeWidth="3.2" strokeLinecap="round" />
      </>
    )
  }

  if (kind === 3) {
    return (
      <>
        <circle cx="39" cy="45" r="5.5" fill={color} />
        <circle cx="61" cy="45" r="5.5" fill={color} />
        <circle cx="37.5" cy="43.2" r="1.2" fill="#ffffff" />
        <circle cx="59.5" cy="43.2" r="1.2" fill="#ffffff" />
      </>
    )
  }

  return (
    <>
      <circle cx="39" cy="45" r="4.4" fill={color} />
      <circle cx="61" cy="45" r="4.4" fill={color} />
    </>
  )
}

function Mouth({ kind, color }) {
  if (kind === 1) {
    return <path d="M36 63 Q50 76 64 63" fill="none" stroke={color} strokeWidth="3.2" strokeLinecap="round" />
  }
  if (kind === 2) {
    return <line x1="37" y1="65" x2="63" y2="65" stroke={color} strokeWidth="3.2" strokeLinecap="round" />
  }
  if (kind === 3) {
    return <path d="M36 64 Q43 60 50 64 Q57 68 64 64" fill="none" stroke={color} strokeWidth="3.2" strokeLinecap="round" />
  }
  return <path d="M36 65 Q50 57 64 65" fill="none" stroke={color} strokeWidth="3.2" strokeLinecap="round" />
}

function Accessory({ kind, color, accent }) {
  if (kind === 1) {
    return (
      <>
        <line x1="50" y1="20" x2="50" y2="28" stroke={color} strokeWidth="2.4" strokeLinecap="round" />
        <circle cx="50" cy="17" r="4.2" fill={accent} />
      </>
    )
  }
  if (kind === 2) {
    return (
      <>
        <path d="M29 37 Q50 28 71 37" fill="none" stroke={accent} strokeWidth="4" strokeLinecap="round" />
        <path d="M31 38 Q50 30 69 38" fill="none" stroke={color} strokeWidth="2" strokeLinecap="round" />
      </>
    )
  }
  if (kind === 3) {
    return (
      <>
        <rect x="24" y="48" width="5" height="14" rx="2" fill={accent} />
        <rect x="71" y="48" width="5" height="14" rx="2" fill={accent} />
      </>
    )
  }
  if (kind === 4) {
    return (
      <path
        d="M18 59 Q16 49 22 42 Q28 35 39 36"
        fill="none"
        stroke={accent}
        strokeWidth="3"
        strokeLinecap="round"
      />
    )
  }
  return null
}

export default function AgentAvatar({ name = 'Agent', size = 'md', className = '' }) {
  const rootRef = useRef(null)
  const faceRef = useRef(null)
  const eyeRef = useRef(null)
  const mouthRef = useRef(null)
  const spec = useMemo(() => createFace(name), [name])

  useEffect(() => {
    const root = rootRef.current
    if (!root) return undefined

    const eyes = eyeRef.current
    const mouth = mouthRef.current
    const face = faceRef.current

    const onEnter = () => {
      gsap.to(face, { rotate: 5, scale: 1.04, y: -1, duration: 0.28, ease: 'power2.out', transformOrigin: '50% 50%' })
      gsap.to(eyes, { scaleY: 0.85, duration: 0.22, ease: 'power2.out', transformOrigin: '50% 50%' })
      gsap.to(mouth, { y: -1, duration: 0.25, ease: 'power2.out' })
    }

    const onLeave = () => {
      gsap.to(face, { rotate: 0, scale: 1, y: 0, duration: 0.35, ease: 'power2.out' })
      gsap.to(eyes, { scaleY: 1, duration: 0.3, ease: 'power2.out' })
      gsap.to(mouth, { y: 0, duration: 0.3, ease: 'power2.out' })
    }

    root.addEventListener('mouseenter', onEnter)
    root.addEventListener('mouseleave', onLeave)
    return () => {
      root.removeEventListener('mouseenter', onEnter)
      root.removeEventListener('mouseleave', onLeave)
    }
  }, [])

  return (
    <span
      ref={rootRef}
      className={`agent-avatar agent-avatar--${size} ${className}`.trim()}
      style={{ '--aa-blink': `${spec.blink}s` }}
      aria-label={`${name} avatar`}
      title={name}
    >
      <svg viewBox="0 0 100 100" role="img" aria-hidden="true">
        <defs>
          <linearGradient id={`aa-grad-${spec.id}`} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor={spec.palette.shell} />
            <stop offset="100%" stopColor={spec.palette.shell2} />
          </linearGradient>
        </defs>

        <g ref={faceRef} transform={`translate(50 50) scale(${spec.faceScale}) translate(-50 -50)`}>
          <rect
            x="18"
            y="22"
            width="64"
            height="60"
            rx={spec.corner}
            fill={`url(#aa-grad-${spec.id})`}
          />
          <rect
            x="21"
            y="25"
            width="58"
            height="54"
            rx={Math.max(6, spec.corner - 4)}
            fill="rgba(255,255,255,0.07)"
          />

          <g className="agent-avatar__eyes" ref={eyeRef}>
            <Eyes kind={spec.eyeKind} color={spec.palette.eye} />
          </g>

          {spec.cheek && (
            <>
              <circle cx="31" cy="57" r="4.2" fill={spec.palette.accent} opacity="0.55" />
              <circle cx="69" cy="57" r="4.2" fill={spec.palette.accent} opacity="0.55" />
            </>
          )}

          <g ref={mouthRef}>
            <Mouth kind={spec.mouthKind} color={spec.palette.eye} />
          </g>

          <Accessory kind={spec.accessory} color={spec.palette.eye} accent={spec.palette.accent} />
        </g>
      </svg>
    </span>
  )
}
