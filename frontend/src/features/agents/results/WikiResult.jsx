import Badge from '../../../ui/Badge'
import Pill from '../../../ui/Pill'
import { ExternalLink } from 'lucide-react'
import './ResultRenderer.css'

const TYPE_VARIANT = {
  person: 'info', place: 'accent', organization: 'warn', technology: 'info',
  concept: 'default', event: 'warn', other: 'default',
}

export default function WikiResult({ result: r }) {
  return (
    <div className="result">
      <div className="result-header">
        <div>
          <p className="result-header__title">{r.title}</p>
          {r.content_type && <Badge label={r.content_type} variant={TYPE_VARIANT[r.content_type]} style={{ marginTop: '6px' }} />}
        </div>
        {r.url && (
          <a href={r.url} target="_blank" rel="noopener noreferrer" style={{ color: 'var(--accent)', display: 'flex', alignItems: 'center', gap: '4px', fontSize: '0.8125rem', textDecoration: 'none', flexShrink: 0 }}>
            Wikipedia <ExternalLink size={12} />
          </a>
        )}
      </div>

      {r.summary && (
        <div className="result-box">
          <p style={{ fontSize: '0.875rem', lineHeight: 1.65 }}>{r.summary}</p>
        </div>
      )}

      {r.key_facts?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Key Facts</p>
          <div className="result-list">
            {r.key_facts.map((f, i) => (
              <div key={i} className="result-list__item">
                <span className="result-list__bullet" />
                {f}
              </div>
            ))}
          </div>
        </div>
      )}

      {r.related_topics?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Related Topics</p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
            {r.related_topics.map((t, i) => <Pill key={i} size="sm">{t}</Pill>)}
          </div>
        </div>
      )}
    </div>
  )
}
