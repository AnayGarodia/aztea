import './ResultRenderer.css'

function asDisplaySrc(mime, value) {
  const raw = String(value ?? '').trim()
  if (!raw) return null
  if (raw.startsWith('data:') || raw.startsWith('http://') || raw.startsWith('https://')) return raw
  if (raw.startsWith('/')) return raw
  if (String(mime ?? '').toLowerCase().includes('/')) {
    return `data:${mime};base64,${raw}`
  }
  return null
}

function isPrimitive(value) {
  return value == null || ['string', 'number', 'boolean'].includes(typeof value)
}

export default function GenericResult({ result }) {
  const output = result ?? {}
  const summary =
    output.summary
    || output.message
    || output.answer
    || output.title
    || output.one_line_summary
    || null
  const artifacts = Array.isArray(output.artifacts)
    ? output.artifacts.filter(item => item && typeof item === 'object')
    : []
  const detailEntries = Object.entries(output).filter(([key]) => key !== 'artifacts' && key !== 'summary')

  return (
    <div className="result">
      {summary && (
        <div className="result-box result-box--accent">
          <p style={{ fontSize: '0.875rem', lineHeight: 1.65 }}>{String(summary)}</p>
        </div>
      )}

      {artifacts.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Artifacts</p>
          <div className="result-media-grid">
            {artifacts.map((artifact, index) => {
              const name = String(artifact.name ?? `artifact-${index + 1}`)
              const mime = String(artifact.mime ?? '').toLowerCase()
              const source = asDisplaySrc(mime, artifact.url_or_base64)
              return (
                <div key={`${name}-${index}`} className="result-media-card">
                  <div className="result-media-card__meta">
                    <span className="result-media-card__name">{name}</span>
                    {mime && <span className="result-media-card__mime">{mime}</span>}
                  </div>
                  {source && mime.startsWith('image/') && (
                    <img className="result-media__image" src={source} alt={name} loading="lazy" />
                  )}
                  {source && mime.startsWith('video/') && (
                    <video className="result-media__video" src={source} controls playsInline />
                  )}
                  {!source && (
                    <p className="result-media-card__fallback">Preview unavailable.</p>
                  )}
                  {source && (
                    <a className="result-media-card__link" href={source} target="_blank" rel="noreferrer">
                      Open artifact
                    </a>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {detailEntries.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Details</p>
          <div className="result-generic-details">
            {detailEntries.map(([key, value]) => (
              <div key={key} className="result-generic-row">
                <p className="result-generic-row__key">{key}</p>
                {isPrimitive(value) ? (
                  <p className="result-generic-row__value">{String(value)}</p>
                ) : (
                  <pre className="result-json">{JSON.stringify(value, null, 2)}</pre>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
