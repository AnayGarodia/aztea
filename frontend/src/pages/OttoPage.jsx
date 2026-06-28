import { useEffect } from 'react'

// Bumped automatically by update.sh on every release — do not edit by hand.
const VERSION = '0.5.1'
const DMG = `Otto-${VERSION}.dmg`
const DMG_HREF = `/otto/${DMG}`

// Brand colours
const BRAND = '#F5A623'   // otto orange
const INK   = '#22183C'   // dark purple-ink

function OttoCreature({ size = 64 }) {
  const r = size * 0.28  // squircle corner radius
  const eyeD = size * 0.22
  const gap  = size * 0.09
  const totalW = eyeD * 2 + gap
  const ex0 = (size - totalW) / 2
  const ex1 = ex0 + eyeD + gap
  const ey  = (size - eyeD) / 2 + size * 0.025
  const pupilD = eyeD * 0.5
  const pd = (eyeD - pupilD) / 2

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} fill="none">
      <rect width={size} height={size} rx={r} ry={r} fill={BRAND} />
      {[ex0, ex1].map((x, i) => (
        <g key={i}>
          <ellipse cx={x + eyeD / 2} cy={ey + eyeD / 2} rx={eyeD / 2} ry={eyeD / 2} fill="white" />
          <ellipse cx={x + pd + pupilD / 2} cy={ey + pd + pupilD / 2} rx={pupilD / 2} ry={pupilD / 2} fill={INK} />
        </g>
      ))}
    </svg>
  )
}

export default function OttoPage() {
  useEffect(() => { document.title = 'Otto – AI agent for your Mac' }, [])

  return (
    <div style={{
      minHeight: '100vh',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      background: '#0f0f0f',
      color: '#f5f0e8',
      fontFamily: 'system-ui, -apple-system, sans-serif',
      padding: '2rem',
      textAlign: 'center',
    }}>
      <div style={{ marginBottom: '1.5rem' }}>
        <OttoCreature size={72} />
      </div>

      <h1 style={{ fontSize: '2rem', fontWeight: 700, margin: '0 0 0.5rem' }}>Otto</h1>
      <p style={{ fontSize: '1.1rem', color: '#a0978a', margin: '0 0 2rem', maxWidth: '380px' }}>
        An AI agent that runs on your Mac — sees your screen, clicks things, and gets work done hands-free.
      </p>

      <a
        href={DMG_HREF}
        download
        style={{
          display: 'inline-block',
          padding: '0.75rem 2rem',
          background: BRAND,
          color: INK,
          borderRadius: '8px',
          fontWeight: 600,
          fontSize: '1rem',
          textDecoration: 'none',
          marginBottom: '0.75rem',
        }}
      >
        Download Otto {VERSION}
      </a>

      <p style={{ fontSize: '0.8rem', color: '#6b6460', margin: 0 }}>
        macOS 14 Sonoma or later · Apple Silicon &amp; Intel
      </p>
    </div>
  )
}
