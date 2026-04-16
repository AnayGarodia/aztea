import React from 'react'
import Button from './Button'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }

  static getDerivedStateFromError() {
    return { hasError: true }
  }

  componentDidCatch(error) {
    // Keep this lightweight; route-level fallback avoids blank screens in production.
    console.error('Route render failed:', error)
  }

  render() {
    if (!this.state.hasError) return this.props.children
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
            Something went wrong while loading this page.
          </h1>
          <p style={{ color: 'var(--ink-soft)', marginBottom: 'var(--sp-4)' }}>
            Refresh to recover. If this keeps happening, try again in a moment.
          </p>
          <Button variant="primary" onClick={() => window.location.reload()}>
            Reload app
          </Button>
        </div>
      </main>
    )
  }
}
