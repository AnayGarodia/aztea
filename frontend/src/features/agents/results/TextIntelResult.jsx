import Badge from '../../../ui/Badge'
import './ResultRenderer.css'

export default function TextIntelResult({ result: r }) {
  const score = r.sentiment_score ?? 0
  // score is -1..1; map to 0..100 for bar (50 = neutral)
  const leftPct  = score < 0 ? 50 - Math.abs(score) * 50 : 50
  const widthPct = Math.abs(score) * 50
  const fillColor = score >= 0 ? 'var(--positive)' : 'var(--negative)'

  return (
    <div className="result">
      <div className="result-header">
        <div>
          <p className="result-header__title">Text Intelligence</p>
          {r.language && <p className="result-header__sub">{r.language}</p>}
        </div>
        {r.sentiment && <Badge label={r.sentiment} dot />}
      </div>

      <div className="result-meta">
        {r.word_count != null && (
          <div className="result-meta__item">
            <span className="result-meta__val">{r.word_count.toLocaleString()}</span>
            <span className="result-meta__key">Words</span>
          </div>
        )}
        {r.reading_time_seconds != null && (
          <div className="result-meta__item">
            <span className="result-meta__val">{Math.ceil(r.reading_time_seconds / 60)}m</span>
            <span className="result-meta__key">Read time</span>
          </div>
        )}
        {r.sentiment_score != null && (
          <div className="result-meta__item">
            <span className="result-meta__val">{r.sentiment_score.toFixed(2)}</span>
            <span className="result-meta__key">Sentiment score</span>
          </div>
        )}
      </div>

      <div className="result-section">
        <p className="result-section__label">Sentiment</p>
        <div className="result-sentiment-bar">
          <div className="result-sentiment-bar__midline" />
          <div
            className="result-sentiment-bar__fill"
            style={{ left: `${leftPct}%`, width: `${widthPct}%`, background: fillColor }}
          />
        </div>
      </div>

      {r.summary && (
        <div className="result-box">
          <p style={{ fontSize: '0.875rem', lineHeight: 1.6 }}>{r.summary}</p>
        </div>
      )}

      {r.key_entities?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Key Entities</p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
            {r.key_entities.map((e, i) => (
              <span key={i} style={{ fontSize: '0.8125rem', padding: '3px 10px', background: 'var(--canvas-sunk)', border: '1px solid var(--line)', borderRadius: 'var(--r-pill)', color: 'var(--ink-soft)' }}>{e}</span>
            ))}
          </div>
        </div>
      )}

      {r.key_quotes?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Key Quotes</p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {r.key_quotes.map((q, i) => (
              <blockquote key={i} style={{ margin: 0, paddingLeft: '12px', borderLeft: '3px solid var(--accent-line)', fontSize: '0.875rem', color: 'var(--ink-soft)', lineHeight: 1.6, fontStyle: 'italic' }}>
                {q}
              </blockquote>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
