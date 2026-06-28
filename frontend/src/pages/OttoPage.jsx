import { useEffect } from 'react'

// Bumped automatically by update.sh on every release — do not edit by hand.
const VERSION = '0.5.0'
const DMG = `Otto-${VERSION}.dmg`
const DMG_HREF = `/otto/${DMG}`

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
      <svg width="64" height="64" viewBox="0 0 64 64" fill="none" style={{ marginBottom: '1.5rem' }}>
        <circle cx="32" cy="32" r="32" fill="#1a1a1a" />
        {/* Octagram: two squares rotated 0° and 45° */}
        {[0, 45].map(deg => {
          const rad = deg * Math.PI / 180
          const r = 22
          const pts = [0, 1, 2, 3].map(i => {
            const a = rad + i * Math.PI / 2
            return `${32 + r * Math.cos(a)},${32 + r * Math.sin(a)}`
          }).join(' ')
          return <polygon key={deg} points={pts} fill="none" stroke="#2dd4bf" strokeWidth="3" strokeLinejoin="round" />
        })}
        <circle cx="32" cy="32" r="5" fill="#c05a3a" />
      </svg>

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
          background: '#2dd4bf',
          color: '#0f0f0f',
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
