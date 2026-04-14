import './ResultRenderer.css'

export default function GenericResult({ result }) {
  return (
    <div className="result">
      <p style={{ fontSize: '0.75rem', fontWeight: 600, letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--ink-mute)', marginBottom: '8px' }}>
        Result
      </p>
      <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
    </div>
  )
}
