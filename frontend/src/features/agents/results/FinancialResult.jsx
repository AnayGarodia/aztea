import Badge from '../../../ui/Badge'
import './ResultRenderer.css'

export default function FinancialResult({ result: r }) {
  const ts = r.generated_at ? new Date(r.generated_at).toLocaleString() : null
  return (
    <div className="result">
      <div className="result-header">
        <div>
          <p className="result-header__title">{r.company_name ?? r.ticker}</p>
          <p className="result-header__sub">{r.filing_type} · {r.filing_date}</p>
        </div>
        {r.signal && <Badge label={r.signal} dot />}
      </div>

      <div className="result-meta">
        <div className="result-meta__item">
          <span className="result-meta__val">{r.ticker}</span>
          <span className="result-meta__key">Ticker</span>
        </div>
        <div className="result-meta__item">
          <span className="result-meta__val">{r.filing_type}</span>
          <span className="result-meta__key">Filing</span>
        </div>
        <div className="result-meta__item">
          <span className="result-meta__val">{r.filing_date ?? '-'}</span>
          <span className="result-meta__key">Date</span>
        </div>
      </div>

      {r.signal_reasoning && (
        <div className={`result-box result-box--${r.signal === 'positive' ? 'positive' : r.signal === 'negative' ? 'negative' : 'accent'}`}>
          <p style={{ fontSize: '0.875rem', lineHeight: 1.6 }}>{r.signal_reasoning}</p>
        </div>
      )}

      {r.business_summary && (
        <div className="result-section">
          <p className="result-section__label">Business Summary</p>
          <p className="result-section__body">{r.business_summary}</p>
        </div>
      )}

      {r.recent_financial_highlights?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Financial Highlights</p>
          <div className="result-list">
            {r.recent_financial_highlights.map((h, i) => (
              <div key={i} className="result-list__item">
                <span className="result-list__bullet" />
                {h}
              </div>
            ))}
          </div>
        </div>
      )}

      {r.key_risks?.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Key Risks</p>
          <div className="result-list">
            {r.key_risks.map((risk, i) => (
              <div key={i} className="result-list__item">
                <span className="result-list__bullet" style={{ background: 'var(--negative)' }} />
                {risk}
              </div>
            ))}
          </div>
        </div>
      )}

      {ts && <p className="result-timestamp">Generated {ts}</p>}
    </div>
  )
}
