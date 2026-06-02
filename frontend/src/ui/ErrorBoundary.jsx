import React from 'react'
import Button from './Button'

// One auto-reload per browsing session, max. A chunk that 404/403s after a
// deploy is fixed by fetching a fresh index.html — but if the asset is
// *persistently* unreachable (e.g. an edge/CDN 403 on /assets/*), an
// unguarded reload becomes an infinite refresh loop that hides the real
// failure. The sessionStorage flag bounds it to a single retry.
const RELOAD_GUARD_KEY = 'aztea:eb-chunk-reloaded'

function isChunkLoadError(error) {
  const msg = error?.message ?? ''
  return (
    msg.includes('Failed to fetch dynamically imported module') ||
    msg.includes('Importing a module script failed') ||
    msg.includes('Loading chunk') ||
    msg.includes('error loading dynamically imported module') ||
    error?.name === 'ChunkLoadError'
  )
}

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null, info: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    // Chunk load errors happen when the browser has a stale index.html after a
    // deploy — the old chunk URL 404s. Auto-reload once fetches fresh
    // index.html. Guarded so a persistently-unreachable chunk (CDN 403, etc.)
    // can't spin the page in a reload loop.
    if (isChunkLoadError(error)) {
      let alreadyReloaded = false
      try {
        alreadyReloaded = sessionStorage.getItem(RELOAD_GUARD_KEY) === '1'
        if (!alreadyReloaded) sessionStorage.setItem(RELOAD_GUARD_KEY, '1')
      } catch (storageErr) {
        // Private mode / disabled storage: fall through to the error UI rather
        // than risk an unbounded reload loop with no way to record the retry.
        console.error('ErrorBoundary: sessionStorage unavailable', storageErr)
        alreadyReloaded = true
      }
      if (!alreadyReloaded) {
        window.location.reload()
        return
      }
    }
    console.error('Route render failed:', error, info?.componentStack)
    this.setState({ info })
  }

  render() {
    if (!this.state.hasError) return this.props.children
    const { error, info } = this.state
    const detail = [error?.message, info?.componentStack]
      .filter(Boolean)
      .join('\n')
    return (
      <main style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        background: 'var(--canvas)',
        padding: 'var(--sp-6)',
      }}>
        <div style={{
          maxWidth: 560,
          width: '100%',
          border: '1px solid var(--line-soft)',
          borderRadius: 'var(--r-md)',
          background: 'var(--surface)',
          padding: 'var(--sp-6)',
        }}>
          <p style={{ fontSize: '0.75rem', color: 'var(--ink-mute)', marginBottom: 'var(--sp-2)' }}>
            Unexpected error
          </p>
          <h1 style={{ fontSize: '1.125rem', marginBottom: 'var(--sp-2)' }}>
            This page didn’t load.
          </h1>
          <p style={{ color: 'var(--ink-soft)', marginBottom: 'var(--sp-4)' }}>
            Refresh to try again. If it keeps happening, the error below is what our team needs.
          </p>
          {detail && (
            <pre style={{
              fontSize: '0.75rem',
              color: 'var(--ink-soft)',
              background: 'var(--canvas)',
              border: '1px solid var(--line-soft)',
              borderRadius: 'var(--r-sm)',
              padding: 'var(--sp-3)',
              marginBottom: 'var(--sp-4)',
              maxHeight: 200,
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}>
              {detail}
            </pre>
          )}
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload app
          </Button>
        </div>
      </main>
    )
  }
}
