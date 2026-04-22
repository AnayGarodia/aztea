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

function parseJsonOrNull(str) {
  const s = (str ?? '').trim()
  if (!s) return {}
  return JSON.parse(s)
}

function tagsFromString(str) {
  return str.split(',').map(t => t.trim()).filter(Boolean)
}

function validateUrl(raw, fieldName) {
  const url = raw.trim()
  if (!url) return `${fieldName} is required.`
  let parsed
  try { parsed = new URL(url) } catch {
    return `${fieldName} must be a valid URL (e.g. https://your-agent.example.com/run).`
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    return `${fieldName} must use https:// or http://.`
  }
  if (parsed.protocol === 'http:') {
    return `${fieldName} should use https:// for security. http:// endpoints will be rejected by most callers.`
  }
  const host = parsed.hostname.toLowerCase()
  if (host === 'localhost' || host === '127.0.0.1' || host === '::1' || host.endsWith('.local')) {
    return `${fieldName} cannot point to localhost or a local address — callers can't reach it.`
  }
  return null
}

function validateForm(form) {
  const name = form.name.trim()
  if (!name) return 'Agent name is required.'
  if (name.length < 3) return 'Agent name must be at least 3 characters.'
  if (name.length > 80) return 'Agent name must be 80 characters or fewer.'

  const desc = form.description.trim()
  if (!desc) return 'Description is required.'
  if (desc.length < 20) return 'Description must be at least 20 characters — help callers understand what your agent does.'
  if (desc.length > 2000) return 'Description must be 2 000 characters or fewer.'

  const urlErr = validateUrl(form.endpoint_url, 'Endpoint URL')
  if (urlErr) return urlErr

  const price = parseFloat(form.price_per_call_usd)
  if (!Number.isFinite(price) || price < 0) return 'Price must be a non-negative number.'
  if (price > 1000) return 'Price per call cannot exceed $1 000.'

  if (form.healthcheck_url.trim()) {
    const hcErr = validateUrl(form.healthcheck_url, 'Healthcheck URL')
    if (hcErr) return hcErr
  }

  return null
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

    const validationError = validateForm(form)
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
      try { inputSchema = parseJsonOrNull(form.input_schema) } catch {
        throw new Error('Input schema must be valid JSON.')
      }
      try { outputSchema = parseJsonOrNull(form.output_schema) } catch {
        throw new Error('Output schema must be valid JSON.')
      }

      const payload = {
        name: form.name.trim(),
        description: form.description.trim(),
        endpoint_url: form.endpoint_url.trim(),
        price_per_call_usd: price,
        tags: tagsFromString(form.tags),
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
                <p className="regagent__success-name">{registered.name ?? form.name}</p>
                {registered.status && registered.status !== 'active' ? (
                  <p className="regagent__success-sub">
                    Your agent is <strong>{registered.status}</strong>
                    {registered.suspension_reason ? ` — ${registered.suspension_reason}` : ' and pending review before it appears publicly.'}
                  </p>
                ) : (
                  <p className="regagent__success-sub">
                    Your agent is live on the marketplace. It may take a few seconds to appear in search.
                  </p>
                )}
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
            <div className="regagent__header">
              <h1 className="regagent__title">Register an agent</h1>
              <p className="regagent__sub">
                List your agent on the marketplace. Callers pay per call; you earn 90% after platform fee.
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
                      value={form.price_per_call_usd}
                      onChange={e => set('price_per_call_usd', e.target.value)}
                      required
                      mono
                      hint="You receive 90% of this after the platform fee."
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
