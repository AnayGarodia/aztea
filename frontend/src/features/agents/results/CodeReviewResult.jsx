import Badge from '../../../ui/Badge'
import './ResultRenderer.css'

const SEV_ORDER = ['critical', 'high', 'medium', 'low', 'info']

export default function CodeReviewResult({ result: r }) {
  const score = r.score ?? 0
  const pct   = (score / 10) * 100
  const issues = [...(r.issues ?? [])].sort((a, b) =>
    SEV_ORDER.indexOf(a.severity) - SEV_ORDER.indexOf(b.severity)
  )

  return (
    <div className="result">
      <div className="result-header">
        <div>
          <p className="result-header__title">Code Review</p>
          {r.language_detected && <p className="result-header__sub">{r.language_detected}</p>}
        </div>
        <div className="result-score">
          <div className="result-score__bar">
            <div className="result-score__fill" style={{ width: `${pct}%` }} />
          </div>
          <span className="result-score__num">{score}/10</span>
        </div>
      </div>

      {r.summary && (
        <div className="result-box">
          <p style={{ fontSize: '0.875rem', lineHeight: 1.6 }}>{r.summary}</p>
        </div>
      )}

      {issues.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Issues ({issues.length})</p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {issues.map((issue, i) => (
              <div key={i} className="result-issue">
                <div className="result-issue__header">
                  <Badge label={issue.severity} dot />
                  {issue.category && <span style={{ fontSize: '0.8125rem', color: 'var(--text-muted)' }}>{issue.category}</span>}
                  {issue.line_hint && <span style={{ fontFamily: 'var(--font-mono)', fontSize: '0.75rem', color: 'var(--text-muted)' }}>line {issue.line_hint}</span>}
                </div>
                <p className="result-issue__desc">{issue.description}</p>
                {issue.fix && <p className="result-issue__fix">Fix: {issue.fix}</p>}
              </div>
            ))}
          </div>
        </div>
      )}

      {r.positive_aspects?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Strengths</p>
          <div className="result-list">
            {r.positive_aspects.map((p, i) => (
              <div key={i} className="result-list__item">
                <span className="result-list__bullet" style={{ background: 'var(--positive)' }} />
                {p}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
