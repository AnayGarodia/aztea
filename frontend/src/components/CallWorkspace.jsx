import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMarket } from '../context/MarketContext'
import { callAgent } from '../api'

// ── Signal badge ──────────────────────────────────────────────────────────────
function SignalBadge({ signal }) {
  const map = {
    positive: { bg: 'var(--positive-bg)', border: 'var(--positive-border)',
      color: 'var(--positive)', icon: '↑', label: 'Positive' },
    neutral:  { bg: 'var(--neutral-bg)',  border: 'var(--neutral-border)',
      color: 'var(--neutral-color)', icon: '→', label: 'Neutral' },
    negative: { bg: 'var(--negative-bg)', border: 'var(--negative-border)',
      color: 'var(--negative)', icon: '↓', label: 'Negative' },
    mixed:    { bg: 'var(--neutral-bg)',  border: 'var(--neutral-border)',
      color: 'var(--neutral-color)', icon: '↔', label: 'Mixed' },
  }
  const s = map[signal] ?? map.neutral
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '4px 12px', borderRadius: 20,
      background: s.bg, border: `1px solid ${s.border}`, color: s.color,
      fontSize: 13, fontWeight: 600,
    }}>
      {s.icon} {s.label}
    </span>
  )
}

// ── Severity badge ────────────────────────────────────────────────────────────
function SeverityBadge({ severity }) {
  const map = {
    critical: { bg: '#FEF2F2', border: '#FECACA', color: '#991B1B' },
    high:     { bg: '#FFF7ED', border: '#FED7AA', color: '#C2410C' },
    medium:   { bg: '#FFFBEB', border: '#FDE68A', color: '#92400E' },
    low:      { bg: '#F0FDF4', border: '#BBF7D0', color: '#166534' },
    info:     { bg: 'var(--brand-light)', border: 'var(--brand-border)', color: 'var(--brand)' },
  }
  const s = map[severity?.toLowerCase()] ?? map.info
  return (
    <span style={{
      fontSize: 10, fontWeight: 700, padding: '2px 7px', borderRadius: 3,
      background: s.bg, border: `1px solid ${s.border}`, color: s.color,
      textTransform: 'uppercase', letterSpacing: '0.05em',
    }}>
      {severity}
    </span>
  )
}

// ── Generic result renderers ──────────────────────────────────────────────────

function FinancialResult({ result }) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', overflow: 'hidden',
        boxShadow: 'var(--shadow-sm)', marginTop: 20,
      }}
    >
      <div style={{ padding: '18px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 22 }}>
              {result.ticker}
            </span>
            <span style={{ fontSize: 14, color: 'var(--text-secondary)' }}>
              {result.company_name}
            </span>
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
            {result.filing_type} · Filed {result.filing_date}
          </div>
        </div>
        <SignalBadge signal={result.signal} />
      </div>
      <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>
        <ResultBlock color={result.signal === 'positive' ? 'var(--positive)' :
          result.signal === 'negative' ? 'var(--negative)' : 'var(--neutral-color)'}
          bg={result.signal === 'positive' ? 'var(--positive-bg)' :
          result.signal === 'negative' ? 'var(--negative-bg)' : 'var(--neutral-bg)'}
          border={result.signal === 'positive' ? 'var(--positive-border)' :
          result.signal === 'negative' ? 'var(--negative-border)' : 'var(--neutral-border)'}>
          {result.signal_reasoning}
        </ResultBlock>
        <Section title="Business summary">
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
            {result.business_summary}
          </p>
        </Section>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <Section title="Financial highlights">
            <BulletList items={result.recent_financial_highlights} color="var(--positive)" />
          </Section>
          <Section title="Key risks">
            <BulletList items={result.key_risks} color="var(--negative)" />
          </Section>
        </div>
        <Timestamp ts={result.generated_at} />
      </div>
    </motion.div>
  )
}

function CodeReviewResult({ result }) {
  const scoreColor = result.score >= 8 ? 'var(--positive)' :
    result.score >= 5 ? 'var(--neutral-color)' : 'var(--negative)'
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', overflow: 'hidden',
        boxShadow: 'var(--shadow-sm)', marginTop: 20,
      }}
    >
      <div style={{ padding: '18px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>Code Review</div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
            Language: <strong>{result.language_detected}</strong>
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 32, fontFamily: 'var(--font-mono)', fontWeight: 800, color: scoreColor }}>
            {result.score}<span style={{ fontSize: 14, color: 'var(--text-muted)' }}>/10</span>
          </div>
        </div>
      </div>
      <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>
        <Section title="Summary">
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
            {result.summary}
          </p>
        </Section>
        {result.issues?.length > 0 && (
          <Section title={`Issues (${result.issues.length})`}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              {result.issues.map((issue, i) => (
                <div key={i} style={{
                  padding: '12px 14px', borderRadius: 'var(--radius-md)',
                  border: '1px solid var(--border)', background: 'var(--surface-subtle)',
                }}>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
                    <SeverityBadge severity={issue.severity} />
                    <span style={{ fontSize: 11, color: 'var(--text-muted)',
                      background: 'var(--border)', padding: '2px 6px', borderRadius: 3 }}>
                      {issue.category}
                    </span>
                    {issue.line_hint && (
                      <code style={{ fontSize: 11, color: 'var(--text-muted)',
                        fontFamily: 'var(--font-mono)', overflow: 'hidden',
                        textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 200 }}>
                        {issue.line_hint}
                      </code>
                    )}
                  </div>
                  <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 6, lineHeight: 1.5 }}>
                    {issue.description}
                  </p>
                  <p style={{ fontSize: 12, color: 'var(--positive)', lineHeight: 1.5 }}>
                    Fix: {issue.fix}
                  </p>
                </div>
              ))}
            </div>
          </Section>
        )}
        {result.positive_aspects?.length > 0 && (
          <Section title="What's good">
            <BulletList items={result.positive_aspects} color="var(--positive)" />
          </Section>
        )}
      </div>
    </motion.div>
  )
}

function TextIntelResult({ result }) {
  const sentColor = result.sentiment === 'positive' ? 'var(--positive)' :
    result.sentiment === 'negative' ? 'var(--negative)' : 'var(--neutral-color)'
  const score = result.sentiment_score ?? 0
  const pct = Math.round(((score + 1) / 2) * 100)

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', overflow: 'hidden',
        boxShadow: 'var(--shadow-sm)', marginTop: 20,
      }}
    >
      <div style={{ padding: '18px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>Text Intelligence</div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
            {result.word_count} words
            {result.reading_time_seconds > 0 && ` · ~${Math.ceil(result.reading_time_seconds / 60)} min read`}
            {result.language && ` · ${result.language.toUpperCase()}`}
          </div>
        </div>
        <SignalBadge signal={result.sentiment} />
      </div>
      <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>
        {/* Sentiment bar */}
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between',
            fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
            <span>Negative</span>
            <span style={{ color: sentColor, fontWeight: 600 }}>
              Score: {score.toFixed(2)}
            </span>
            <span>Positive</span>
          </div>
          <div style={{ height: 6, background: 'var(--border)', borderRadius: 3, overflow: 'hidden' }}>
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${pct}%` }}
              transition={{ duration: 0.7, ease: [0.25, 0.1, 0.25, 1] }}
              style={{ height: '100%', background: sentColor, borderRadius: 3 }}
            />
          </div>
        </div>
        <Section title="Summary">
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
            {result.summary}
          </p>
        </Section>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {result.key_entities?.length > 0 && (
            <Section title="Key entities">
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {result.key_entities.map((e, i) => (
                  <span key={i} style={{
                    fontSize: 12, padding: '3px 9px', borderRadius: 20,
                    background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
                    color: 'var(--brand)',
                  }}>{e}</span>
                ))}
              </div>
            </Section>
          )}
          {result.main_topics?.length > 0 && (
            <Section title="Topics">
              <BulletList items={result.main_topics} color="var(--brand)" />
            </Section>
          )}
        </div>
        {result.key_quotes?.length > 0 && (
          <Section title="Key quotes">
            {result.key_quotes.map((q, i) => (
              <blockquote key={i} style={{
                borderLeft: '3px solid var(--brand-border)',
                paddingLeft: 14, margin: '0 0 8px',
                fontSize: 13, color: 'var(--text-secondary)',
                fontStyle: 'italic', lineHeight: 1.6,
              }}>
                "{q}"
              </blockquote>
            ))}
          </Section>
        )}
      </div>
    </motion.div>
  )
}

function WikiResult({ result }) {
  const typeColors = {
    person: { bg: '#EDE9FE', border: '#C4B5FD', color: '#6D28D9' },
    place: { bg: '#ECFDF5', border: '#6EE7B7', color: '#065F46' },
    organization: { bg: '#FFF7ED', border: '#FED7AA', color: '#9A3412' },
    concept: { bg: 'var(--brand-light)', border: 'var(--brand-border)', color: 'var(--brand)' },
    event: { bg: '#FEF2F2', border: '#FECACA', color: '#991B1B' },
    technology: { bg: '#F0F9FF', border: '#BAE6FD', color: '#0369A1' },
    other: { bg: 'var(--surface-subtle)', border: 'var(--border)', color: 'var(--text-secondary)' },
  }
  const tc = typeColors[result.content_type] ?? typeColors.other

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', overflow: 'hidden',
        boxShadow: 'var(--shadow-sm)', marginTop: 20,
      }}
    >
      <div style={{ padding: '18px 24px', borderBottom: '1px solid var(--border)',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: 16 }}>{result.title}</div>
          {result.url && (
            <a href={result.url} target="_blank" rel="noopener noreferrer"
              style={{ fontSize: 12, color: 'var(--brand)', marginTop: 2, display: 'block' }}>
              {result.url.replace('https://', '')} ↗
            </a>
          )}
        </div>
        <span style={{
          fontSize: 11, fontWeight: 700, padding: '4px 10px', borderRadius: 20,
          background: tc.bg, border: `1px solid ${tc.border}`, color: tc.color,
          textTransform: 'capitalize',
        }}>
          {result.content_type}
        </span>
      </div>
      <div style={{ padding: '20px 24px', display: 'flex', flexDirection: 'column', gap: 20 }}>
        <Section title="Summary">
          <p style={{ fontSize: 14, color: 'var(--text-secondary)', lineHeight: 1.7 }}>
            {result.summary}
          </p>
        </Section>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {result.key_facts?.length > 0 && (
            <Section title="Key facts">
              <BulletList items={result.key_facts} color="var(--brand)" />
            </Section>
          )}
          {result.related_topics?.length > 0 && (
            <Section title="Related topics">
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {result.related_topics.map((t, i) => (
                  <span key={i} style={{
                    fontSize: 12, padding: '3px 9px', borderRadius: 20,
                    background: 'var(--surface-subtle)', border: '1px solid var(--border)',
                    color: 'var(--text-secondary)',
                  }}>{t}</span>
                ))}
              </div>
            </Section>
          )}
        </div>
      </div>
    </motion.div>
  )
}

// ── Generic JSON fallback ─────────────────────────────────────────────────────
function GenericResult({ result }) {
  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35 }}
      style={{
        marginTop: 20, background: 'var(--surface)',
        border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)',
        overflow: 'hidden', boxShadow: 'var(--shadow-sm)',
      }}
    >
      <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)',
        fontSize: 12, fontWeight: 600, color: 'var(--text-muted)',
        textTransform: 'uppercase', letterSpacing: '0.06em' }}>
        Response
      </div>
      <pre style={{
        padding: 20, fontSize: 12, fontFamily: 'var(--font-mono)',
        color: 'var(--text-primary)', overflowX: 'auto', lineHeight: 1.6,
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      }}>
        {JSON.stringify(result, null, 2)}
      </pre>
    </motion.div>
  )
}

// ── Shared UI primitives ──────────────────────────────────────────────────────
function Section({ title, children }) {
  return (
    <div>
      <h4 style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)',
        textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 10 }}>
        {title}
      </h4>
      {children}
    </div>
  )
}

function BulletList({ items, color }) {
  return (
    <ul style={{ listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 6 }}>
      {(items ?? []).map((item, i) => (
        <motion.li
          key={i}
          initial={{ opacity: 0, x: -6 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.04 }}
          style={{ display: 'flex', gap: 8, alignItems: 'flex-start',
            fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.5 }}
        >
          <span style={{ color, flexShrink: 0, marginTop: 4, fontSize: 8 }}>●</span>
          {item}
        </motion.li>
      ))}
    </ul>
  )
}

function ResultBlock({ color, bg, border, children }) {
  return (
    <div style={{
      padding: '14px 16px', borderRadius: 'var(--radius-md)',
      background: bg, border: `1px solid ${border}`,
      fontSize: 14, lineHeight: 1.6, color,
    }}>
      {children}
    </div>
  )
}

function Timestamp({ ts }) {
  if (!ts) return null
  return (
    <div style={{ fontSize: 11, color: 'var(--text-muted)', textAlign: 'right' }}>
      Generated {new Date(ts).toLocaleString()}
    </div>
  )
}

// ── Loading skeleton ──────────────────────────────────────────────────────────
function LoadingSkeleton() {
  return (
    <motion.div
      initial={{ opacity: 0 }} animate={{ opacity: 1 }}
      style={{
        marginTop: 20, background: 'var(--surface)',
        border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)',
        padding: 24, display: 'flex', flexDirection: 'column', gap: 16,
      }}
    >
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }`}</style>
      {[['35%', 22], ['100%', 52], ['90%', 14], ['75%', 14], ['80%', 14]].map(([w, h], i) => (
        <div key={i} style={{
          width: w, height: h, borderRadius: 4,
          background: 'var(--border)', animation: 'pulse 1.5s ease-in-out infinite',
          animationDelay: `${i * 0.1}s`,
        }} />
      ))}
    </motion.div>
  )
}

// ── Dynamic input form ────────────────────────────────────────────────────────
function AgentInputForm({ agent, onSubmit, loading }) {
  const fields = agent.input_schema?.fields ?? []
  const [values, setValues] = useState(() =>
    Object.fromEntries(fields.map(f => [f.name, f.default ?? '']))
  )

  const set = (name) => (e) => {
    let v = e.target.value
    setValues(prev => ({ ...prev, [name]: v }))
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    if (loading) return
    // Apply transforms
    const out = {}
    for (const f of fields) {
      let v = values[f.name] ?? ''
      if (f.transform === 'uppercase') v = v.toString().toUpperCase()
      out[f.name] = v || f.default || ''
    }
    onSubmit(out)
  }

  const inputBase = {
    width: '100%', padding: '10px 14px',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-md)',
    background: 'var(--bg)',
    fontSize: 14, color: 'var(--text-primary)',
    outline: 'none',
    transition: 'border-color 0.15s',
    fontFamily: 'var(--font-sans)',
  }

  const canSubmit = fields
    .filter(f => f.required)
    .every(f => String(values[f.name] ?? '').trim())

  return (
    <form onSubmit={handleSubmit}
      style={{
        background: 'var(--surface)', border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)', padding: 20, boxShadow: 'var(--shadow-sm)',
        display: 'flex', flexDirection: 'column', gap: 14,
      }}
    >
      {fields.map(f => (
        <div key={f.name}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
            <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text-secondary)' }}>
              {f.label}
              {f.required && <span style={{ color: 'var(--negative)', marginLeft: 3 }}>*</span>}
            </label>
            {f.hint && (
              <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>{f.hint}</span>
            )}
          </div>

          {f.type === 'textarea' ? (
            <textarea
              placeholder={f.placeholder ?? ''}
              value={values[f.name] ?? ''}
              onChange={set(f.name)}
              disabled={loading}
              rows={6}
              maxLength={f.max_length}
              style={{ ...inputBase, resize: 'vertical', minHeight: 120, lineHeight: 1.5 }}
            />
          ) : f.type === 'select' ? (
            <select
              value={values[f.name] ?? f.default ?? ''}
              onChange={set(f.name)}
              disabled={loading}
              style={inputBase}
            >
              {(f.options ?? []).map(opt => (
                <option key={opt} value={opt}>{opt}</option>
              ))}
            </select>
          ) : (
            <input
              type="text"
              placeholder={f.placeholder ?? ''}
              value={values[f.name] ?? ''}
              onChange={set(f.name)}
              disabled={loading}
              maxLength={f.max_length ?? 200}
              style={{
                ...inputBase,
                fontFamily: f.transform === 'uppercase' ? 'var(--font-mono)' : 'var(--font-sans)',
                fontWeight: f.transform === 'uppercase' ? 700 : 400,
                letterSpacing: f.transform === 'uppercase' ? '0.08em' : 'normal',
              }}
            />
          )}
        </div>
      ))}

      <button
        type="submit"
        disabled={loading || !canSubmit}
        style={{
          padding: '11px 0', borderRadius: 'var(--radius-md)',
          background: loading || !canSubmit ? 'var(--border)' : 'var(--brand)',
          color: loading || !canSubmit ? 'var(--text-muted)' : 'white',
          fontSize: 14, fontWeight: 600,
          cursor: loading || !canSubmit ? 'not-allowed' : 'pointer',
          transition: 'background 0.15s, color 0.15s',
        }}
      >
        {loading ? 'Running…' : `Call agent  ($${agent.price_per_call_usd.toFixed(3)})`}
      </button>
    </form>
  )
}

// ── Result picker — choose renderer based on agent tags ───────────────────────
function ResultView({ result, agent }) {
  const tags = agent.tags ?? []
  if (tags.includes('financial-research') || tags.includes('sec-filings'))
    return <FinancialResult result={result} />
  if (tags.includes('code-review') || tags.includes('security'))
    return <CodeReviewResult result={result} />
  if (tags.includes('nlp') || tags.includes('sentiment-analysis') || tags.includes('text-analytics'))
    return <TextIntelResult result={result} />
  if (tags.includes('wikipedia') || tags.includes('research') || tags.includes('knowledge-base'))
    return <WikiResult result={result} />
  return <GenericResult result={result} />
}

// ── Main workspace ─────────────────────────────────────────────────────────────
export default function CallWorkspace({ agent }) {
  const { apiKey, showToast, refreshWallet, refresh } = useMarket()
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)   // { data } | { error, status }
  const [lastAgentId, setLastAgentId] = useState(null)

  if (agent?.agent_id !== lastAgentId) {
    setLastAgentId(agent?.agent_id)
    setResult(null)
  }

  const handleSubmit = async (payload) => {
    setLoading(true)
    setResult(null)
    try {
      const { status, ok, body } = await callAgent(apiKey, agent.agent_id, payload)
      if (ok) {
        setResult({ data: body })
        showToast(`Agent returned successfully`, 'success')
      } else if (status === 402) {
        const d = body.detail ?? {}
        setResult({
          error: `Insufficient balance. Have ${d.balance_cents ?? 0}¢, need ${d.required_cents ?? 1}¢.`,
          status,
        })
        showToast('Not enough balance — add funds', 'error')
      } else {
        const msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
        setResult({ error: msg, status })
        showToast(`Call failed (${status})`, 'error')
      }
    } catch (err) {
      setResult({ error: err.message, status: 0 })
      showToast('Network error', 'error')
    } finally {
      setLoading(false)
      setTimeout(() => { refreshWallet(); refresh() }, 500)
    }
  }

  return (
    <div style={{ maxWidth: 760, paddingBottom: 28 }}>
      {/* Agent header */}
      <div style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h1 style={{ fontSize: 19, fontWeight: 700, letterSpacing: '-0.02em',
              marginBottom: 4, color: 'var(--text-primary)' }}>
              {agent.name}
            </h1>
            <p style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.55,
              maxWidth: 560 }}>
              {agent.description}
            </p>
          </div>
          <div style={{ textAlign: 'right', flexShrink: 0, marginLeft: 20 }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 20,
              color: 'var(--text-primary)' }}>
              ${agent.price_per_call_usd.toFixed(3)}
            </div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>per call</div>
          </div>
        </div>

        {/* Stats row */}
        <div style={{ display: 'flex', gap: 24, marginTop: 12, flexWrap: 'wrap' }}>
          {[
            { label: 'Total calls', value: agent.total_calls },
            { label: 'Success rate', value: agent.total_calls > 0 ? `${Math.round(agent.success_rate * 100)}%` : '—' },
            { label: 'Avg latency', value: agent.avg_latency_ms > 0 ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—' },
          ].map(s => (
            <div key={s.label}>
              <div style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 600,
                textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 2 }}>
                {s.label}
              </div>
              <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)',
                fontFamily: typeof s.value === 'number' ? 'var(--font-mono)' : undefined }}>
                {s.value}
              </div>
            </div>
          ))}
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            {(agent.tags ?? []).map(tag => (
              <span key={tag} style={{
                fontSize: 10, padding: '2px 8px', borderRadius: 3,
                background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
                color: 'var(--brand)', fontWeight: 500,
              }}>
                {tag}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Dynamic input form */}
      <AgentInputForm agent={agent} onSubmit={handleSubmit} loading={loading} />

      {/* Result area */}
      <AnimatePresence mode="wait">
        {loading && <LoadingSkeleton key="loading" />}
        {!loading && result?.error && (
          <motion.div key="error"
            initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
            style={{
              marginTop: 20, padding: '16px 20px',
              background: 'var(--negative-bg)', border: '1px solid var(--negative-border)',
              borderRadius: 'var(--radius-lg)', fontSize: 14,
              color: 'var(--negative)', lineHeight: 1.5,
            }}
          >
            <strong>Error{result.status > 0 ? ` (${result.status})` : ''}:</strong> {result.error}
          </motion.div>
        )}
        {!loading && result?.data && (
          <ResultView key="result" result={result.data} agent={agent} />
        )}
      </AnimatePresence>
    </div>
  )
}
