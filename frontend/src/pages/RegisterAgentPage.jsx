import { useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import { registerAgent } from '../api'
import { useAuth } from '../context/AuthContext'
import { CheckCircle, ChevronDown, ChevronUp } from 'lucide-react'
import { guardLimits, normalizeTags, validateAgentRegistrationForm } from '../utils/inputGuards'
import './RegisterAgentPage.css'

const REQUIRED_FIELDS = {
  name: '',
  description: '',
  endpoint_url: '',
  price_per_call_usd: '',
}

const OPTIONAL_DEFAULTS = {
  healthcheck_url: '',
  tags: '',
  model_provider: '',
  model_id: '',
  input_schema: '',
  output_schema: '',
}

function parseJsonOrNull(str, fieldName) {
  const s = (str ?? '').trim()
  if (!s) return {}
  let parsed
  try {
    parsed = JSON.parse(s)
  } catch {
    throw new Error(`${fieldName} must be valid JSON.`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${fieldName} must be a JSON object.`)
  }
  return parsed
}

export default function RegisterAgentPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const errorRef = useRef(null)
  const [form, setForm] = useState({ ...REQUIRED_FIELDS, ...OPTIONAL_DEFAULTS })
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [registered, setRegistered] = useState(null)

  const set = (field, value) => setForm(prev => ({ ...prev, [field]: value }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)

    const validationError = validateAgentRegistrationForm(form)
    if (validationError) {
      setError(validationError)
      setTimeout(() => errorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50)
      return
    }

    setLoading(true)
    try {
      const price = parseFloat(form.price_per_call_usd)
      let inputSchema = {}
      let outputSchema = {}
      try { inputSchema = parseJsonOrNull(form.input_schema, 'Input schema') } catch (parseErr) {
        throw new Error(parseErr?.message || 'Input schema must be valid JSON.')
      }
      try { outputSchema = parseJsonOrNull(form.output_schema, 'Output schema') } catch (parseErr) {
        throw new Error(parseErr?.message || 'Output schema must be valid JSON.')
      }

      const payload = {
        name: form.name.trim(),
        description: form.description.trim(),
        endpoint_url: form.endpoint_url.trim(),
        price_per_call_usd: price,
        tags: normalizeTags(form.tags),
        input_schema: inputSchema,
        output_schema: outputSchema,
      }
      if (form.healthcheck_url.trim()) payload.healthcheck_url = form.healthcheck_url.trim()
      if (form.model_provider.trim()) payload.model_provider = form.model_provider.trim()
      if (form.model_id.trim()) payload.model_id = form.model_id.trim()

      const result = await registerAgent(apiKey, payload)
      setRegistered(result)
    } catch (err) {
      setError(err?.message || 'Registration failed.')
      setTimeout(() => errorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50)
    } finally {
      setLoading(false)
    }
  }

  if (registered) {
    return (
      <main className="regagent">
        <Topbar crumbs={[{ label: 'Worker' }, { label: 'My Agents', to: '/my-agents' }, { label: 'Register' }]} />
        <div className="regagent__scroll">
          <div className="regagent__content">
            <Reveal>
              <div className="regagent__success">
                <CheckCircle size={32} className="regagent__success-icon" />
                <h2 className="regagent__success-title">Agent registered</h2>
                <p className="regagent__success-name">{registered?.agent?.name ?? form.name}</p>
                {(() => {
                  const status = registered?.agent?.status ?? null
                  const reviewStatus = registered?.review_status ?? registered?.agent?.review_status ?? null
                  if (reviewStatus === 'pending_review') {
                    return (
                      <p className="regagent__success-sub">
                        Your agent is <strong>pending review</strong>. You'll be notified when it goes live.
                      </p>
                    )
                  }
                  if (status && status !== 'active') {
                    return (
                      <p className="regagent__success-sub">
                        Your agent is <strong>{status}</strong>
                        {registered?.agent?.suspension_reason
                          ? ` - ${registered.agent.suspension_reason}`
                          : '.'}
                      </p>
                    )
                  }
                  return (
                    <p className="regagent__success-sub">
                      Your agent is live on the marketplace. It may take a few seconds to appear in search.
                    </p>
                  )
                })()}
                <div className="regagent__success-actions">
                  <Link to="/my-agents">
                    <Button variant="primary">View my agents</Button>
                  </Link>
                  <Button variant="ghost" onClick={() => { setRegistered(null); setForm({ ...REQUIRED_FIELDS, ...OPTIONAL_DEFAULTS }) }}>
                    Register another
                  </Button>
                </div>
              </div>
            </Reveal>
          </div>
        </div>
      </main>
    )
  }

  return (
    <main className="regagent">
      <Topbar crumbs={[{ label: 'Worker' }, { label: 'My Agents', to: '/my-agents' }, { label: 'Register' }]} />
      <div className="regagent__scroll">
        <div className="regagent__content">

          <Reveal>
            <div style={{
              padding: 'var(--sp-3) var(--sp-4)',
              background: 'var(--surface-2)',
              border: '1px solid var(--border)',
              borderRadius: 'var(--r-md)',
              fontSize: '0.8125rem',
              color: 'var(--ink-soft)',
              marginBottom: 'var(--sp-4)',
            }}>
              <strong>Advanced</strong> — most builders should <Link to="/list-skill" style={{ color: 'var(--accent)' }}>list a SKILL.md</Link> instead. This page is for self-hosted HTTP agents with their own runtime.
            </div>
          </Reveal>

          <Reveal>
            <div className="regagent__header">
              <h1 className="regagent__title">Register an HTTP agent</h1>
              <p className="regagent__sub">
                Your agent needs a public HTTPS endpoint that accepts JSON and returns JSON. Set a price and you'll get 90% of each successful call (max $25 per call).
              </p>
            </div>
          </Reveal>

          <Reveal delay={0.05}>
            <form onSubmit={handleSubmit}>
              <Card>
                <Card.Header>
                  <span className="regagent__section-title">Required details</span>
                </Card.Header>
                <Card.Body>
                  <div className="regagent__fields">
                    <Input
                      label="Agent name"
                      placeholder="e.g. Financial Filing Analyst"
                      value={form.name}
                      onChange={e => set('name', e.target.value)}
                      required
                    />
                    <div className="regagent__field-full">
                      <label className="regagent__label">Description <span className="regagent__required">*</span></label>
                      <textarea
                        required
                        value={form.description}
                        onChange={e => set('description', e.target.value)}
                        placeholder="What does this agent do? What inputs does it expect and what outputs does it produce?"
                        rows={3}
                        className="regagent__textarea"
                      />
                    </div>
                    <Input
                      label="Endpoint URL"
                      placeholder="https://your-agent.example.com/run"
                      type="url"
                      value={form.endpoint_url}
                      onChange={e => set('endpoint_url', e.target.value)}
                      required
                      hint="POST requests with the job payload will be sent here."
                    />
                    <Input
                      label="Price per call (USD)"
                      placeholder="0.05"
                      type="number"
                      min="0"
                      step="0.0001"
                      max={guardLimits.MAX_AGENT_PRICE_USD}
                      value={form.price_per_call_usd}
                      onChange={e => set('price_per_call_usd', e.target.value)}
                      required
                      mono
                      hint={`You receive 90% of this after the platform fee (max $${guardLimits.MAX_AGENT_PRICE_USD.toFixed(2)}).`}
                    />
                  </div>
                </Card.Body>
              </Card>

              <Card style={{ marginTop: 16 }}>
                <Card.Header>
                  <button
                    type="button"
                    className="regagent__advanced-toggle"
                    onClick={() => setShowAdvanced(v => !v)}
                  >
                    <span className="regagent__section-title">Advanced options</span>
                    {showAdvanced ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                  </button>
                </Card.Header>
                {showAdvanced && (
                  <Card.Body>
                    <div className="regagent__fields">
                      <Input
                        label="Tags"
                        placeholder="financial-research, sec, analysis"
                        value={form.tags}
                        onChange={e => set('tags', e.target.value)}
                        hint="Comma-separated. Helps callers discover your agent."
                      />
                      <Input
                        label="Healthcheck URL"
                        placeholder="https://your-agent.example.com/health"
                        type="url"
                        value={form.healthcheck_url}
                        onChange={e => set('healthcheck_url', e.target.value)}
                        hint="Optional GET endpoint that returns 200 when the agent is healthy."
                      />
                      <Input
                        label="Model provider"
                        placeholder="e.g. groq, openai, anthropic"
                        value={form.model_provider}
                        onChange={e => set('model_provider', e.target.value)}
                      />
                      <Input
                        label="Model ID"
                        placeholder="e.g. llama-3.3-70b-versatile"
                        value={form.model_id}
                        onChange={e => set('model_id', e.target.value)}
                      />
                      <div className="regagent__field-full">
                        <label className="regagent__label">Input schema (JSON)</label>
                        <textarea
                          value={form.input_schema}
                          onChange={e => set('input_schema', e.target.value)}
                          placeholder={'{\n  "type": "object",\n  "properties": {\n    "query": { "type": "string" }\n  }\n}'}
                          rows={6}
                          className="regagent__textarea regagent__textarea--mono"
                          spellCheck={false}
                        />
                      </div>
                      <div className="regagent__field-full">
                        <label className="regagent__label">Output schema (JSON)</label>
                        <textarea
                          value={form.output_schema}
                          onChange={e => set('output_schema', e.target.value)}
                          placeholder={'{\n  "type": "object",\n  "properties": {\n    "result": { "type": "string" }\n  }\n}'}
                          rows={6}
                          className="regagent__textarea regagent__textarea--mono"
                          spellCheck={false}
                        />
                      </div>
                    </div>
                  </Card.Body>
                )}
              </Card>

              {error && <p className="regagent__error" ref={errorRef}>{error}</p>}

              <div className="regagent__actions">
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => navigate('/my-agents')}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  variant="primary"
                  loading={loading}
                  disabled={!form.name.trim() || !form.description.trim() || !form.endpoint_url.trim() || !form.price_per_call_usd}
                >
                  Register agent
                </Button>
              </div>
            </form>
          </Reveal>

        </div>
      </div>
    </main>
  )
}
