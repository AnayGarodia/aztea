import { useEffect, useState, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useMarket } from '../context/MarketContext'
import { callAgent, createJob, getJob } from '../api'

function detailToText(detail) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail == null) return null
  if (Array.isArray(detail)) {
    const joined = detail.map(item => detailToText(item) ?? '').filter(Boolean).join(', ')
    return joined || null
  }
  if (typeof detail === 'object') {
    if (typeof detail.error === 'string' && detail.error) return detail.error
    return JSON.stringify(detail)
  }
  return String(detail)
}

function formatCallError(status, body) {
  if (body && typeof body === 'object' && body.detail !== undefined) {
    const detailMsg = detailToText(body.detail)
    if (detailMsg) return detailMsg
  }
  if (typeof body === 'string' && body.trim()) return body
  return status > 0 ? `HTTP ${status}` : 'Network error'
}

// ── Signal badge ──────────────────────────────────────────────────────────────
function SignalBadge({ signal }) {
  const map = {
    positive: { bg: 'var(--positive-bg)', border: 'var(--positive-border)', color: 'var(--positive)', icon: '↑', label: 'Positive' },
    neutral:  { bg: 'var(--neutral-bg)',  border: 'var(--neutral-border)',  color: 'var(--neutral-color)', icon: '→', label: 'Neutral' },
    negative: { bg: 'var(--negative-bg)', border: 'var(--negative-border)', color: 'var(--negative)', icon: '↓', label: 'Negative' },
    mixed:    { bg: 'var(--neutral-bg)',  border: 'var(--neutral-border)',  color: 'var(--neutral-color)', icon: '↔', label: 'Mixed' },
  }
  const s = map[signal] ?? map.neutral
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '4px 11px', borderRadius: 20,
      background: s.bg, border: `1px solid ${s.border}`, color: s.color,
      fontSize: 12, fontWeight: 700,
    }}>
      {s.icon} {s.label}
    </span>
  )
}

// ── Severity badge ────────────────────────────────────────────────────────────
function SeverityBadge({ severity }) {
  const map = {
    critical: { bg: 'rgba(240,82,82,0.12)', border: 'rgba(240,82,82,0.3)', color: '#F87171' },
    high:     { bg: 'rgba(245,158,11,0.1)', border: 'rgba(245,158,11,0.25)', color: '#FBBF24' },
    medium:   { bg: 'rgba(245,158,11,0.07)', border: 'rgba(245,158,11,0.2)', color: '#F59E0B' },
    low:      { bg: 'var(--positive-bg)', border: 'var(--positive-border)', color: 'var(--positive)' },
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

// ── Shared primitives ─────────────────────────────────────────────────────────
function Section({ title, children }) {
  return (
    <div>
      <h4 style={{ fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.08em', marginBottom: 10 }}>
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
        <motion.li key={i} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}
          transition={{ delay: i * 0.04 }}
          style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.5 }}>
          <span style={{ color, flexShrink: 0, marginTop: 5, fontSize: 6 }}>●</span>
          {item}
        </motion.li>
      ))}
    </ul>
  )
}

function ResultBlock({ color, bg, border, children }) {
  return (
    <div style={{ padding: '13px 16px', borderRadius: 'var(--radius-md)', background: bg, border: `1px solid ${border}`, fontSize: 13.5, lineHeight: 1.65, color }}>
      {children}
    </div>
  )
}

function Timestamp({ ts }) {
  if (!ts) return null
  return (
    <div style={{ fontSize: 10.5, color: 'var(--text-muted)', textAlign: 'right', fontFamily: 'var(--font-mono)' }}>
      Generated {new Date(ts).toLocaleString()}
    </div>
  )
}

function CardShell({ children, style }) {
  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3 }}
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        overflow: 'hidden',
        boxShadow: 'var(--shadow-sm)',
        marginTop: 18,
        ...style,
      }}
    >
      {children}
    </motion.div>
  )
}

// ── Result renderers ──────────────────────────────────────────────────────────
function FinancialResult({ result }) {
  return (
    <CardShell>
      <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 20 }}>{result.ticker}</span>
            <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>{result.company_name}</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
            {result.filing_type} · Filed {result.filing_date}
          </div>
        </div>
        <SignalBadge signal={result.signal} />
      </div>
      <div style={{ padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 18 }}>
        <ResultBlock
          color={result.signal === 'positive' ? 'var(--positive)' : result.signal === 'negative' ? 'var(--negative)' : 'var(--neutral-color)'}
          bg={result.signal === 'positive' ? 'var(--positive-bg)' : result.signal === 'negative' ? 'var(--negative-bg)' : 'var(--neutral-bg)'}
          border={result.signal === 'positive' ? 'var(--positive-border)' : result.signal === 'negative' ? 'var(--negative-border)' : 'var(--neutral-border)'}
        >
          {result.signal_reasoning}
        </ResultBlock>
        <Section title="Business summary">
          <p style={{ fontSize: 13.5, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{result.business_summary}</p>
        </Section>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          <Section title="Financial highlights"><BulletList items={result.recent_financial_highlights} color="var(--positive)" /></Section>
          <Section title="Key risks"><BulletList items={result.key_risks} color="var(--negative)" /></Section>
        </div>
        <Timestamp ts={result.generated_at} />
      </div>
    </CardShell>
  )
}

function CodeReviewResult({ result }) {
  const scoreColor = result.score >= 8 ? 'var(--positive)' : result.score >= 5 ? 'var(--neutral-color)' : 'var(--negative)'
  return (
    <CardShell>
      <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 15 }}>Code Review</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>Language: <strong style={{ color: 'var(--text-secondary)' }}>{result.language_detected}</strong></div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 30, fontFamily: 'var(--font-mono)', fontWeight: 800, color: scoreColor, lineHeight: 1 }}>
            {result.score}<span style={{ fontSize: 13, color: 'var(--text-muted)' }}>/10</span>
          </div>
        </div>
      </div>
      <div style={{ padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 18 }}>
        <Section title="Summary">
          <p style={{ fontSize: 13.5, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{result.summary}</p>
        </Section>
        {result.issues?.length > 0 && (
          <Section title={`Issues (${result.issues.length})`}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {result.issues.map((issue, i) => (
                <div key={i} style={{ padding: '11px 13px', borderRadius: 'var(--radius-md)', border: '1px solid var(--border)', background: 'var(--surface-2)' }}>
                  <div style={{ display: 'flex', gap: 7, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }}>
                    <SeverityBadge severity={issue.severity} />
                    <span style={{ fontSize: 10, color: 'var(--text-muted)', background: 'var(--bg)', padding: '2px 6px', borderRadius: 3, border: '1px solid var(--border)' }}>{issue.category}</span>
                    {issue.line_hint && (
                      <code style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {issue.line_hint}
                      </code>
                    )}
                  </div>
                  <p style={{ fontSize: 12.5, color: 'var(--text-secondary)', marginBottom: 5, lineHeight: 1.5 }}>{issue.description}</p>
                  <p style={{ fontSize: 11.5, color: 'var(--positive)', lineHeight: 1.5 }}>Fix: {issue.fix}</p>
                </div>
              ))}
            </div>
          </Section>
        )}
        {result.positive_aspects?.length > 0 && (
          <Section title="What's good"><BulletList items={result.positive_aspects} color="var(--positive)" /></Section>
        )}
      </div>
    </CardShell>
  )
}

function TextIntelResult({ result }) {
  const sentColor = result.sentiment === 'positive' ? 'var(--positive)' : result.sentiment === 'negative' ? 'var(--negative)' : 'var(--neutral-color)'
  const score = result.sentiment_score ?? 0
  const pct = Math.round(((score + 1) / 2) * 100)
  return (
    <CardShell>
      <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 15 }}>Text Intelligence</div>
          <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>
            {result.word_count} words{result.reading_time_seconds > 0 && ` · ~${Math.ceil(result.reading_time_seconds / 60)} min read`}{result.language && ` · ${result.language.toUpperCase()}`}
          </div>
        </div>
        <SignalBadge signal={result.sentiment} />
      </div>
      <div style={{ padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 18 }}>
        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>
            <span>Negative</span>
            <span style={{ color: sentColor, fontWeight: 700 }}>Score: {score.toFixed(2)}</span>
            <span>Positive</span>
          </div>
          <div style={{ height: 5, background: 'var(--border)', borderRadius: 3, overflow: 'hidden' }}>
            <motion.div initial={{ width: 0 }} animate={{ width: `${pct}%` }} transition={{ duration: 0.7 }}
              style={{ height: '100%', background: sentColor, borderRadius: 3 }} />
          </div>
        </div>
        <Section title="Summary"><p style={{ fontSize: 13.5, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{result.summary}</p></Section>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {result.key_entities?.length > 0 && (
            <Section title="Key entities">
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                {result.key_entities.map((e, i) => (
                  <span key={i} style={{ fontSize: 11.5, padding: '2px 8px', borderRadius: 20, background: 'var(--brand-light)', border: '1px solid var(--brand-border)', color: 'var(--brand)' }}>{e}</span>
                ))}
              </div>
            </Section>
          )}
          {result.main_topics?.length > 0 && (
            <Section title="Topics"><BulletList items={result.main_topics} color="var(--brand)" /></Section>
          )}
        </div>
        {result.key_quotes?.length > 0 && (
          <Section title="Key quotes">
            {result.key_quotes.map((q, i) => (
              <blockquote key={i} style={{ borderLeft: '3px solid var(--brand-border)', paddingLeft: 13, margin: '0 0 8px', fontSize: 12.5, color: 'var(--text-secondary)', fontStyle: 'italic', lineHeight: 1.6 }}>
                "{q}"
              </blockquote>
            ))}
          </Section>
        )}
      </div>
    </CardShell>
  )
}

function WikiResult({ result }) {
  const typeColors = {
    person:       { bg: 'rgba(139,92,246,0.1)', border: 'rgba(139,92,246,0.25)', color: '#A78BFA' },
    place:        { bg: 'var(--positive-bg)', border: 'var(--positive-border)', color: 'var(--positive)' },
    organization: { bg: 'var(--neutral-bg)', border: 'var(--neutral-border)', color: 'var(--neutral-color)' },
    concept:      { bg: 'var(--brand-light)', border: 'var(--brand-border)', color: 'var(--brand)' },
    event:        { bg: 'var(--negative-bg)', border: 'var(--negative-border)', color: 'var(--negative)' },
    technology:   { bg: 'rgba(124,158,255,0.1)', border: 'rgba(124,158,255,0.25)', color: '#7C9EFF' },
    other:        { bg: 'var(--surface-2)', border: 'var(--border)', color: 'var(--text-secondary)' },
  }
  const tc = typeColors[result.content_type] ?? typeColors.other
  return (
    <CardShell>
      <div style={{ padding: '16px 22px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 15 }}>{result.title}</div>
          {result.url && (
            <a href={result.url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 11, color: 'var(--brand)', marginTop: 2, display: 'block' }}>
              {result.url.replace('https://', '')} ↗
            </a>
          )}
        </div>
        <span style={{ fontSize: 10, fontWeight: 700, padding: '4px 10px', borderRadius: 20, background: tc.bg, border: `1px solid ${tc.border}`, color: tc.color, textTransform: 'capitalize' }}>
          {result.content_type}
        </span>
      </div>
      <div style={{ padding: '18px 22px', display: 'flex', flexDirection: 'column', gap: 18 }}>
        <Section title="Summary"><p style={{ fontSize: 13.5, color: 'var(--text-secondary)', lineHeight: 1.7 }}>{result.summary}</p></Section>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
          {result.key_facts?.length > 0 && <Section title="Key facts"><BulletList items={result.key_facts} color="var(--brand)" /></Section>}
          {result.related_topics?.length > 0 && (
            <Section title="Related topics">
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                {result.related_topics.map((t, i) => (
                  <span key={i} style={{ fontSize: 11.5, padding: '2px 8px', borderRadius: 20, background: 'var(--surface-2)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}>{t}</span>
                ))}
              </div>
            </Section>
          )}
        </div>
      </div>
    </CardShell>
  )
}

function GenericResult({ result }) {
  const rendered =
    typeof result === 'string'
      ? result
      : JSON.stringify(result, null, 2)
  return (
    <CardShell>
      <div style={{ padding: '12px 18px', borderBottom: '1px solid var(--border)', fontSize: 10, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.07em' }}>
        Response
      </div>
      <pre style={{ padding: 18, fontSize: 12, fontFamily: 'var(--font-mono)', color: 'var(--text-primary)', overflowX: 'auto', lineHeight: 1.7, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {rendered}
      </pre>
    </CardShell>
  )
}

function ResultView({ result, agent }) {
  const tags = agent.tags ?? []
  if (tags.includes('financial-research') || tags.includes('sec-filings')) return <FinancialResult result={result} />
  if (tags.includes('code-review') || tags.includes('security')) return <CodeReviewResult result={result} />
  if (tags.includes('nlp') || tags.includes('sentiment-analysis') || tags.includes('text-analytics')) return <TextIntelResult result={result} />
  if (tags.includes('wikipedia') || tags.includes('research') || tags.includes('knowledge-base')) return <WikiResult result={result} />
  return <GenericResult result={result} />
}

// ── Job status badge ───────────────────────────────────────────────────────────
function JobStatusBadge({ status }) {
  const map = {
    pending:               { color: 'var(--neutral-color)', bg: 'var(--neutral-bg)', border: 'var(--neutral-border)', label: 'Pending' },
    running:               { color: 'var(--brand)', bg: 'var(--brand-light)', border: 'var(--brand-border)', label: 'Running' },
    awaiting_clarification:{ color: '#A78BFA', bg: 'rgba(139,92,246,0.1)', border: 'rgba(139,92,246,0.25)', label: 'Waiting' },
    complete:              { color: 'var(--positive)', bg: 'var(--positive-bg)', border: 'var(--positive-border)', label: 'Complete' },
    failed:                { color: 'var(--negative)', bg: 'var(--negative-bg)', border: 'var(--negative-border)', label: 'Failed' },
  }
  const s = map[status] ?? map.pending
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 5,
      padding: '3px 10px', borderRadius: 20,
      background: s.bg, border: `1px solid ${s.border}`, color: s.color,
      fontSize: 11, fontWeight: 700,
    }}>
      {status === 'running' && <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--brand)', animation: 'pulse 1.2s infinite' }} />}
      {s.label}
    </span>
  )
}

// ── Async job tracker ──────────────────────────────────────────────────────────
function JobTracker({ jobId, agent }) {
  const { apiKey, refreshJobs, refreshWallet } = useMarket()
  const [job, setJob] = useState(null)
  const pollRef = useRef(null)

  useEffect(() => {
    const poll = async () => {
      try {
        const j = await getJob(apiKey, jobId)
        setJob(j)
        if (j.status === 'complete' || j.status === 'failed') {
          clearInterval(pollRef.current)
          refreshJobs()
          refreshWallet()
        }
      } catch {}
    }
    poll()
    pollRef.current = setInterval(poll, 3000)
    return () => clearInterval(pollRef.current)
  }, [apiKey, jobId, refreshJobs, refreshWallet])

  if (!job) return (
    <CardShell>
      <div style={{ padding: '18px 22px', display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--brand)', animation: 'pulse 1s infinite' }} />
        <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>Submitting job…</span>
      </div>
    </CardShell>
  )

  return (
    <CardShell>
      <div style={{ padding: '14px 20px', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 13 }}>Async Job</div>
          <code style={{ fontSize: 10, color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{job.job_id?.slice(0, 18)}…</code>
        </div>
        <JobStatusBadge status={job.status} />
      </div>
      <div style={{ padding: '14px 20px' }}>
        {job.status === 'complete' && job.output_payload ? (
          <ResultView result={job.output_payload} agent={agent} />
        ) : job.status === 'failed' ? (
          <div style={{ fontSize: 13, color: 'var(--negative)', padding: '10px 14px', background: 'var(--negative-bg)', borderRadius: 'var(--radius-md)', border: '1px solid var(--negative-border)' }}>
            {job.error_message || 'Job failed.'}
          </div>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13, color: 'var(--text-muted)' }}>
            <div style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--brand)', animation: 'pulse 1s infinite' }} />
            Polling every 3 seconds…
          </div>
        )}
      </div>
    </CardShell>
  )
}

// ── Loading skeleton ───────────────────────────────────────────────────────────
function LoadingSkeleton() {
  return (
    <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
      style={{ marginTop: 18, background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 'var(--radius-lg)', padding: 22, display: 'flex', flexDirection: 'column', gap: 14 }}>
      {[['40%', 20], ['100%', 48], ['88%', 13], ['75%', 13], ['82%', 13]].map(([w, h], i) => (
        <div key={i} style={{ width: w, height: h, borderRadius: 4, background: 'var(--surface-2)', animation: 'pulse 1.5s ease-in-out infinite', animationDelay: `${i * 0.1}s` }} />
      ))}
    </motion.div>
  )
}

// ── Dynamic input form ─────────────────────────────────────────────────────────
function AgentInputForm({ agent, onSubmit, loading, mode, onModeChange }) {
  const fields = agent.input_schema?.fields ?? []
  const [values, setValues] = useState(() =>
    Object.fromEntries(fields.map(f => [f.name, f.default ?? '']))
  )

  useEffect(() => {
    setValues(Object.fromEntries(fields.map(f => [f.name, f.default ?? ''])))
  }, [agent.agent_id])

  const set = name => e => setValues(prev => ({ ...prev, [name]: e.target.value }))

  const handleSubmit = e => {
    e.preventDefault()
    if (loading) return
    const out = {}
    for (const f of fields) {
      let v = values[f.name] ?? ''
      if (f.transform === 'uppercase') v = v.toString().toUpperCase()
      out[f.name] = v || f.default || ''
    }
    onSubmit(out)
  }

  const canSubmit = fields.filter(f => f.required).every(f => String(values[f.name] ?? '').trim())

  const inputStyle = {
    width: '100%', padding: '10px 13px',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-md)',
    background: 'var(--bg)',
    fontSize: 13.5, color: 'var(--text-primary)',
    outline: 'none', transition: 'border-color 0.15s, box-shadow 0.15s',
    fontFamily: 'var(--font-sans)',
  }

  return (
    <form onSubmit={handleSubmit}
      style={{
        background: 'var(--surface)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-lg)',
        padding: '18px 20px',
        boxShadow: 'var(--shadow-sm)',
        display: 'flex', flexDirection: 'column', gap: 14,
      }}
    >
      {fields.map(f => (
        <div key={f.name}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
            <label style={{ fontSize: 11, fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              {f.label}{f.required && <span style={{ color: 'var(--negative)', marginLeft: 2 }}>*</span>}
            </label>
            {f.hint && <span style={{ fontSize: 10.5, color: 'var(--text-muted)' }}>{f.hint}</span>}
          </div>

          {f.type === 'textarea' ? (
            <textarea placeholder={f.placeholder ?? ''} value={values[f.name] ?? ''}
              onChange={set(f.name)} disabled={loading} rows={6} maxLength={f.max_length}
              style={{ ...inputStyle, resize: 'vertical', minHeight: 120, lineHeight: 1.5 }} />
          ) : f.type === 'select' ? (
            <select value={values[f.name] ?? f.default ?? ''} onChange={set(f.name)} disabled={loading} style={inputStyle}>
              {(f.options ?? []).map(opt => <option key={opt} value={opt}>{opt}</option>)}
            </select>
          ) : (
            <input type="text" placeholder={f.placeholder ?? ''} value={values[f.name] ?? ''}
              onChange={set(f.name)} disabled={loading} maxLength={f.max_length ?? 200}
              style={{
                ...inputStyle,
                fontFamily: f.transform === 'uppercase' ? 'var(--font-mono)' : 'var(--font-sans)',
                fontWeight: f.transform === 'uppercase' ? 700 : 400,
                letterSpacing: f.transform === 'uppercase' ? '0.08em' : 'normal',
                textTransform: f.transform === 'uppercase' ? 'uppercase' : 'none',
              }}
            />
          )}
        </div>
      ))}

      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        {/* Mode toggle */}
        <div style={{
          display: 'flex', borderRadius: 'var(--radius-md)',
          border: '1px solid var(--border)',
          background: 'var(--bg)',
          overflow: 'hidden', flexShrink: 0,
        }}>
          {[{ id: 'sync', label: 'Sync' }, { id: 'async', label: 'Async' }].map(m => (
            <button
              type="button"
              key={m.id}
              onClick={() => onModeChange(m.id)}
              style={{
                padding: '7px 12px', fontSize: 11.5, fontWeight: 600, cursor: 'pointer',
                background: mode === m.id ? 'var(--surface-2)' : 'transparent',
                color: mode === m.id ? 'var(--brand)' : 'var(--text-muted)',
                border: 'none', transition: 'all 0.15s', fontFamily: 'var(--font-sans)',
              }}
            >
              {m.label}
            </button>
          ))}
        </div>

        <button type="submit" disabled={loading || !canSubmit}
          style={{
            flex: 1, padding: '10px 0', borderRadius: 'var(--radius-md)',
            background: loading || !canSubmit ? 'var(--surface-2)' : 'var(--brand)',
            color: loading || !canSubmit ? 'var(--text-muted)' : 'var(--text-inverse)',
            fontSize: 13.5, fontWeight: 700,
            cursor: loading || !canSubmit ? 'not-allowed' : 'pointer',
            transition: 'all 0.15s', border: 'none',
            fontFamily: 'var(--font-sans)',
          }}
        >
          {loading
            ? mode === 'async' ? 'Submitting job…' : 'Running…'
            : `${mode === 'async' ? 'Submit job' : 'Call agent'} · $${agent.price_per_call_usd.toFixed(3)}`}
        </button>
      </div>
    </form>
  )
}

// ── Main workspace ─────────────────────────────────────────────────────────────
export default function CallWorkspace({ agent }) {
  const { apiKey, showToast, refreshWallet, refresh } = useMarket()
  const [loading, setLoading]   = useState(false)
  const [result,  setResult]    = useState(null)  // { data } | { error, status } | { jobId }
  const [mode, setMode]         = useState('sync')

  useEffect(() => { setResult(null) }, [agent?.agent_id])

  const handleSubmit = async payload => {
    setLoading(true); setResult(null)
    try {
        if (mode === 'async') {
          const job = await createJob(apiKey, agent.agent_id, payload)
          setResult({ jobId: job.job_id })
          showToast('Job submitted', 'info')
        } else {
          const { status, ok, body } = await callAgent(apiKey, agent.agent_id, payload)
          if (ok) {
            setResult({ data: body })
            showToast('Agent returned successfully', 'success')
          } else if (status === 402 && body && typeof body === 'object' && typeof body.detail === 'object') {
            const d = body.detail
            setResult({
              error: `Insufficient balance. Have $${((d.balance_cents ?? 0) / 100).toFixed(2)}, need $${((d.required_cents ?? 1) / 100).toFixed(2)}.`,
              status,
            })
            showToast('Not enough balance — add funds', 'error')
          } else {
            const msg = formatCallError(status, body)
            setResult({ error: msg, status })
            showToast(`Call failed (${status})`, 'error')
          }
        }
      } catch (err) {
        setResult({ error: err.message || 'Network error', status: err.status || 0 })
        showToast(err.message || 'Network error', 'error')
      } finally {
        setLoading(false)
        setTimeout(() => { refreshWallet(); refresh() }, 500)
      }
  }

  return (
    <div style={{ maxWidth: 780, paddingBottom: 28 }}>
      {/* Agent header */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h1 style={{ fontFamily: 'var(--font-display)', fontSize: 18, fontWeight: 800, letterSpacing: '-0.025em', marginBottom: 4 }}>
              {agent.name}
            </h1>
            <p style={{ fontSize: 12.5, color: 'var(--text-secondary)', lineHeight: 1.6, maxWidth: 560 }}>
              {agent.description}
            </p>
          </div>
          <div style={{ textAlign: 'right', flexShrink: 0, marginLeft: 20 }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontWeight: 700, fontSize: 18, color: 'var(--brand)' }}>
              ${agent.price_per_call_usd.toFixed(3)}
            </div>
            <div style={{ fontSize: 10, color: 'var(--text-muted)', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>per call</div>
          </div>
        </div>

        {/* Stats row */}
        <div style={{ display: 'flex', gap: 20, marginTop: 12, flexWrap: 'wrap', alignItems: 'center' }}>
          {[
            { label: 'Total calls', value: agent.total_calls },
            { label: 'Success rate', value: agent.total_calls > 0 ? `${Math.round(agent.success_rate * 100)}%` : '—' },
            { label: 'Avg latency', value: agent.avg_latency_ms > 0 ? `${(agent.avg_latency_ms / 1000).toFixed(1)}s` : '—' },
          ].map(s => (
            <div key={s.label}>
              <div style={{ fontSize: 9.5, color: 'var(--text-muted)', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.07em', marginBottom: 2 }}>{s.label}</div>
              <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-primary)', fontFamily: typeof s.value === 'number' ? 'var(--font-mono)' : undefined }}>{s.value}</div>
            </div>
          ))}
          <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginLeft: 4 }}>
            {(agent.tags ?? []).map(tag => (
              <span key={tag} style={{ fontSize: 9.5, padding: '2px 7px', borderRadius: 3, background: 'var(--brand-light)', border: '1px solid var(--brand-border)', color: 'var(--brand)', fontWeight: 700, letterSpacing: '0.03em' }}>
                {tag}
              </span>
            ))}
          </div>
        </div>
      </div>

      {/* Input form */}
      <AgentInputForm agent={agent} onSubmit={handleSubmit} loading={loading} mode={mode} onModeChange={setMode} />

      {/* Result area */}
      <AnimatePresence mode="wait">
        {loading && <LoadingSkeleton key="loading" />}
        {!loading && result?.error && (
          <motion.div key="error"
            initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
            style={{
              marginTop: 18, padding: '14px 18px',
              background: 'var(--negative-bg)', border: '1px solid var(--negative-border)',
              borderRadius: 'var(--radius-lg)', fontSize: 13.5,
              color: 'var(--negative)', lineHeight: 1.5,
            }}
          >
            <strong>Error{result.status > 0 ? ` (${result.status})` : ''}:</strong> {result.error}
          </motion.div>
        )}
        {!loading && result?.jobId && (
          <JobTracker key={result.jobId} jobId={result.jobId} agent={agent} />
        )}
        {!loading && result?.data && (
          <ResultView key="result" result={result.data} agent={agent} />
        )}
      </AnimatePresence>
    </div>
  )
}
