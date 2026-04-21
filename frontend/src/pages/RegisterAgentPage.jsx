import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Reveal from '../ui/motion/Reveal'
import { registerAgent } from '../api'
import { useAuth } from '../context/AuthContext'
import { ChevronDown, ChevronUp } from 'lucide-react'
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

export default function RegisterAgentPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({ ...REQUIRED_FIELDS, ...OPTIONAL_DEFAULTS })
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const set = (field, value) => setForm(prev => ({ ...prev, [field]: value }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    setError(null)
    setLoading(true)

    try {
      const price = parseFloat(form.price_per_call_usd)
      if (!Number.isFinite(price) || price < 0) throw new Error('Price must be a non-negative number.')

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

      await registerAgent(apiKey, payload)
      navigate('/my-agents')
    } catch (err) {
      setError(err?.message || 'Registration failed.')
    } finally {
      setLoading(false)
    }
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

              {error && <p className="regagent__error">{error}</p>}

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
