import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { registerAgent } from '../api'
import { useMarket } from '../context/MarketContext'

const EXAMPLE_TAGS = ['nlp', 'code-review', 'research', 'financial', 'data', 'image', 'audio', 'developer-tools']

export default function RegisterAgentModal({ onClose }) {
  const { apiKey, refresh, showToast } = useMarket()
  const [form, setForm] = useState({
    name: '',
    description: '',
    endpoint_url: '',
    price_per_call_usd: '0.010',
    tags: '',
    input_schema: '{"fields":[]}',
  })
  const [working, setWorking] = useState(false)
  const [error, setError] = useState('')

  const set = k => e => setForm(f => ({ ...f, [k]: e.target.value }))

  const submit = async e => {
    e.preventDefault()
    setError(''); setWorking(true)
    try {
      const tags = form.tags.split(',').map(t => t.trim()).filter(Boolean)
      let parsedSchema = {}
      if (form.input_schema.trim()) {
        try {
          parsedSchema = JSON.parse(form.input_schema)
        } catch {
          throw new Error('input_schema must be valid JSON.')
        }
        if (typeof parsedSchema !== 'object' || parsedSchema === null || Array.isArray(parsedSchema)) {
          throw new Error('input_schema must be a JSON object.')
        }
      }
      await registerAgent(apiKey, {
        name: form.name.trim(),
        description: form.description.trim(),
        endpoint_url: form.endpoint_url.trim(),
        price_per_call_usd: parseFloat(form.price_per_call_usd),
        tags,
        input_schema: parsedSchema,
      })
      await refresh()
      showToast('Agent registered successfully', 'success')
      onClose()
    } catch (err) {
      setError(err.message)
    } finally { setWorking(false) }
  }

  const inputStyle = {
    width: '100%', padding: '10px 13px',
    background: 'var(--bg)',
    border: '1px solid var(--border)',
    borderRadius: 'var(--radius-md)',
    fontSize: 13, color: 'var(--text-primary)',
    outline: 'none', transition: 'border-color 0.15s, box-shadow 0.15s',
    fontFamily: 'var(--font-sans)',
  }
  const labelStyle = {
    fontSize: 11, fontWeight: 700, color: 'var(--text-muted)',
    display: 'block', marginBottom: 6,
    letterSpacing: '0.05em', textTransform: 'uppercase',
  }

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0, zIndex: 200,
          background: 'rgba(0,0,0,0.7)', backdropFilter: 'blur(6px)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          padding: 24,
        }}
      >
        <motion.div
          initial={{ opacity: 0, y: 20, scale: 0.96 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 10, scale: 0.97 }}
          transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
          onClick={e => e.stopPropagation()}
          style={{
            background: 'var(--surface)',
            border: '1px solid var(--border-bright)',
            borderRadius: 'var(--radius-xl)',
            boxShadow: 'var(--shadow-lg)',
            width: '100%', maxWidth: 520,
            overflow: 'hidden',
          }}
        >
          {/* Header */}
          <div style={{
            padding: '18px 22px',
            borderBottom: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <div>
              <h3 style={{ fontFamily: 'var(--font-display)', fontSize: 16, fontWeight: 700 }}>
                Register an agent
              </h3>
              <p style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 2 }}>
                You'll earn 90% of every successful call
              </p>
            </div>
            <button onClick={onClose} style={{
              width: 28, height: 28, borderRadius: 'var(--radius-md)',
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              color: 'var(--text-muted)', background: 'var(--surface-2)',
              border: '1px solid var(--border)', cursor: 'pointer',
              transition: 'color 0.15s',
            }}>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>

          {/* Form */}
          <form onSubmit={submit} style={{ padding: '20px 22px', display: 'flex', flexDirection: 'column', gap: 14 }}>
            <div>
              <label style={labelStyle}>Agent name</label>
              <input type="text" placeholder="My Specialized Agent" value={form.name}
                onChange={set('name')} required style={inputStyle} />
            </div>

            <div>
              <label style={labelStyle}>Description</label>
              <textarea placeholder="What does your agent do? Be specific about inputs and outputs."
                value={form.description} onChange={set('description')} required
                rows={3}
                style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.5 }} />
            </div>

            <div>
              <label style={labelStyle}>Endpoint URL</label>
              <input type="url" placeholder="https://your-agent.example.com/invoke"
                value={form.endpoint_url} onChange={set('endpoint_url')} required style={inputStyle} />
              <p style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 5 }}>
                Must accept POST with JSON body. Responds with JSON.
              </p>
            </div>

            <div>
              <label style={labelStyle}>Price per call (USD)</label>
              <div style={{ position: 'relative' }}>
                <span style={{
                  position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)',
                  color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 13,
                }}>$</span>
                <input type="number" step="0.001" min="0" placeholder="0.010"
                  value={form.price_per_call_usd} onChange={set('price_per_call_usd')} required
                  style={{ ...inputStyle, paddingLeft: 28, fontFamily: 'var(--font-mono)' }} />
              </div>
            </div>

            <div>
              <label style={labelStyle}>Tags (comma-separated)</label>
              <input type="text" placeholder="nlp, sentiment-analysis, text-analytics"
                value={form.tags} onChange={set('tags')} style={inputStyle} />
              <div style={{ marginTop: 8, display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {EXAMPLE_TAGS.map(tag => (
                  <button
                    type="button"
                    key={tag}
                    onClick={() => {
                      const current = form.tags.split(',').map(t => t.trim()).filter(Boolean)
                      if (!current.includes(tag)) {
                        setForm(f => ({ ...f, tags: [...current, tag].join(', ') }))
                      }
                    }}
                    style={{
                      fontSize: 10, padding: '2px 8px', borderRadius: 3,
                      background: 'var(--brand-light)', border: '1px solid var(--brand-border)',
                      color: 'var(--brand)', fontWeight: 600, cursor: 'pointer',
                      letterSpacing: '0.03em',
                    }}
                  >
                    {tag}
                  </button>
                ))}
              </div>
            </div>

            <div>
              <label style={labelStyle}>Input schema (JSON object)</label>
              <textarea
                value={form.input_schema}
                onChange={set('input_schema')}
                rows={4}
                style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.5, fontFamily: 'var(--font-mono)', fontSize: 12 }}
              />
              <p style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 5 }}>
                Example:{' '}
                <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
                  {"{\"fields\":[{\"name\":\"prompt\",\"type\":\"textarea\",\"required\":true}]}"}
                </code>
              </p>
            </div>

            {error && (
              <div style={{
                padding: '10px 13px', background: 'var(--negative-bg)',
                border: '1px solid var(--negative-border)',
                borderRadius: 'var(--radius-md)', fontSize: 13, color: 'var(--negative)',
              }}>
                {error}
              </div>
            )}

            <div style={{ display: 'flex', gap: 10, paddingTop: 4 }}>
              <button type="button" onClick={onClose}
                style={{
                  flex: 1, padding: '10px 0', borderRadius: 'var(--radius-md)',
                  background: 'var(--surface-2)', border: '1px solid var(--border)',
                  color: 'var(--text-secondary)', fontSize: 13, fontWeight: 600,
                  cursor: 'pointer', fontFamily: 'var(--font-sans)',
                }}>
                Cancel
              </button>
              <button type="submit" disabled={working} className="btn-brand" style={{ flex: 2 }}>
                {working ? 'Registering…' : 'Register agent →'}
              </button>
            </div>
          </form>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  )
}
