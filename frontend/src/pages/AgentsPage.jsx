import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import AgentCard from '../features/agents/AgentCard'
import EmptyState from '../ui/EmptyState'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Textarea from '../ui/Textarea'
import Pill from '../ui/Pill'
import Dialog from '../ui/Dialog'
import Skeleton from '../ui/Skeleton'
import { registerAgent, searchAgents } from '../api'
import { useMarket } from '../context/MarketContext'
import { Plus, Search } from 'lucide-react'
import './AgentsPage.css'

const ALL = '__all__'

function parseSchema(raw, fieldLabel) {
  const trimmed = raw.trim()
  if (!trimmed) return {}
  let parsed
  try {
    parsed = JSON.parse(trimmed)
  } catch {
    throw new Error(`${fieldLabel} must be valid JSON.`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${fieldLabel} must be a JSON object.`)
  }
  return parsed
}

function RegisterDialog({ apiKey, onClose, onSuccess, showToast }) {
  const [form, setForm] = useState({
    name: '',
    description: '',
    endpoint_url: '',
    price_per_call_usd: '0.01',
    tags: '',
    input_schema_text: '',
    output_schema_text: '',
  })
  const [loading, setLoading] = useState(false)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const useSchemaExample = () => {
    set('input_schema_text', JSON.stringify({
      type: 'object',
      properties: {
        task: { type: 'string', description: 'What the agent should do' },
      },
      required: ['task'],
    }, null, 2))
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const tags = form.tags.split(',').map(t => t.trim()).filter(Boolean)
      const input_schema = parseSchema(form.input_schema_text, 'Input schema')
      const output_schema = parseSchema(form.output_schema_text, 'Output schema')

      await registerAgent(apiKey, {
        name: form.name.trim(),
        description: form.description.trim(),
        endpoint_url: form.endpoint_url.trim(),
        price_per_call_usd: parseFloat(form.price_per_call_usd) || 0.01,
        tags,
        input_schema,
        output_schema,
      })
      showToast?.('Agent registered and listed.', 'success')
      onSuccess()
    } catch (err) {
      showToast?.(err?.message ?? 'Registration failed.', 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open title="Register agent listing" onClose={onClose}>
      <Dialog.Body>
        <div className="agents-register__guide">
          <p>Required for launch-ready listings:</p>
          <ul>
            <li>Public HTTPS endpoint that accepts JSON POST input.</li>
            <li>Clear tags and description so callers can discover your agent.</li>
            <li>Input/output JSON schemas so callers know request/response shape.</li>
          </ul>
        </div>

        <form onSubmit={handleSubmit} id="register-form" className="agents-register__form">
          <Input
            label="Agent name"
            value={form.name}
            onChange={e => set('name', e.target.value)}
            required
            placeholder="Financial Filing Analyst"
          />
          <Input
            label="Short description"
            value={form.description}
            onChange={e => set('description', e.target.value)}
            placeholder="Summarizes SEC filings into investment briefs"
            hint="What can a first-time caller expect?"
          />
          <Input
            label="Endpoint URL"
            type="url"
            value={form.endpoint_url}
            onChange={e => set('endpoint_url', e.target.value)}
            required
            placeholder="https://my-agent.example.com/invoke"
            hint="Must be reachable by Agentmarket."
          />
          <Input
            label="Price per call (USD)"
            type="number"
            step="0.001"
            min="0"
            value={form.price_per_call_usd}
            onChange={e => set('price_per_call_usd', e.target.value)}
            required
          />
          <Input
            label="Tags"
            value={form.tags}
            onChange={e => set('tags', e.target.value)}
            placeholder="financial-research, sec, investment"
            hint="Comma-separated discovery labels."
          />

          <div className="agents-register__schema-header">
            <span>Input schema (optional but strongly recommended)</span>
            <button type="button" onClick={useSchemaExample}>Use example</button>
          </div>
          <Textarea
            mono
            value={form.input_schema_text}
            onChange={e => set('input_schema_text', e.target.value)}
            placeholder='{"type":"object","properties":{"task":{"type":"string"}}}'
            hint="JSON object. Define fields callers must send."
            style={{ minHeight: 120 }}
          />

          <Textarea
            label="Output schema (optional)"
            mono
            value={form.output_schema_text}
            onChange={e => set('output_schema_text', e.target.value)}
            placeholder='{"type":"object","properties":{"summary":{"type":"string"}}}'
            hint="JSON object. Helps callers validate responses."
            style={{ minHeight: 120 }}
          />

          <div className="agents-register__actions">
            <Button variant="ghost" type="button" onClick={onClose}>Cancel</Button>
            <Button variant="primary" type="submit" loading={loading}>Register listing</Button>
          </div>
        </form>
      </Dialog.Body>
    </Dialog>
  )
}

export default function AgentsPage() {
  const { agents, loading, apiKey, refresh, showToast } = useMarket()
  const [search, setSearch] = useState('')
  const [activeTag, setActiveTag] = useState(ALL)
  const [showRegister, setShowRegister] = useState(false)
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')

  useEffect(() => {
    const query = search.trim()
    setSearchError('')
    if (!query) {
      setSearchResults([])
      setSearchLoading(false)
      return
    }

    let cancelled = false
    const timer = setTimeout(async () => {
      setSearchLoading(true)
      try {
        const data = await searchAgents(apiKey, query)
        if (cancelled) return
        const normalized = (data?.results ?? []).map(item => {
          const reasonFromList = Array.isArray(item?.match_reasons)
            ? item.match_reasons.find(reason => typeof reason === 'string' && reason.trim())
            : null
          const matchReason = (typeof item?.match_reason === 'string' && item.match_reason.trim())
            ? item.match_reason.trim()
            : (reasonFromList || null)
          return {
            ...(item?.agent ?? {}),
            match_reason: matchReason,
          }
        })
        setSearchResults(normalized)
      } catch (err) {
        if (cancelled) return
        setSearchResults([])
        setSearchError(err?.message ?? 'Search failed.')
      } finally {
        if (!cancelled) setSearchLoading(false)
      }
    }, 250)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [apiKey, search])

  const allTags = useMemo(() => {
    const s = new Set()
    agents.forEach(a => (a.tags ?? []).forEach(t => s.add(t)))
    return [...s].sort()
  }, [agents])

  const filtered = useMemo(() => {
    const source = search.trim() ? searchResults : agents
    return source.filter(a => activeTag === ALL || (a.tags ?? []).includes(activeTag))
  }, [agents, search, searchResults, activeTag])

  const isFiltered = Boolean(search || activeTag !== ALL)
  const listLoading = loading || searchLoading

  return (
    <main className="agents-page">
      <Topbar crumbs={[{ label: 'Agents' }]} />

      <div className="agents-page__scroll">
        <div className="agents-page__content">
          <header className="agents-page__header">
            <div>
              <p className="agents-page__eyebrow">Discover + hire</p>
              <h1>Agent marketplace</h1>
              <p>
                Find specialists by capability, inspect trust signals, and invoke with confidence.
                {loading ? '' : ` ${agents.length} listing${agents.length !== 1 ? 's' : ''} live.`}
              </p>
            </div>
            <div className="agents-page__header-actions">
              <Link to="/jobs">
                <Button variant="secondary" size="sm">Monitor jobs</Button>
              </Link>
              <Button
                variant="primary"
                size="sm"
                icon={<Plus size={14} />}
                onClick={() => setShowRegister(true)}
              >
                Register agent
              </Button>
            </div>
          </header>

          <section className="agents-page__narrative">
            <article>
              <h2>How to hire safely</h2>
              <ul>
                <li>Compare trust score, price, and latency before invoking.</li>
                <li>Use sync for immediate output, async if work can take time.</li>
                <li>Track in Jobs and review wallet settlement history.</li>
              </ul>
            </article>
            <article>
              <h2>How to list your own agent</h2>
              <ul>
                <li>Register endpoint + tags + price.</li>
                <li>Provide input/output JSON schemas for caller clarity.</li>
                <li>Keep response contracts stable to build reputation.</li>
              </ul>
            </article>
          </section>

          <section className="agents-page__filters">
            <Input
              placeholder="Search by name, description, or tag…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              iconLeft={<Search size={14} />}
              hint={searchError || 'Tip: try tags like financial-research, code-review, or text-intel.'}
            />
            <div className="agents-page__tag-row">
              <Pill interactive active={activeTag === ALL} onClick={() => setActiveTag(ALL)}>
                All tags
              </Pill>
              {allTags.map(tag => (
                <Pill key={tag} interactive active={activeTag === tag} onClick={() => setActiveTag(tag)}>
                  {tag}
                </Pill>
              ))}
            </div>
          </section>

          {listLoading ? (
            <div className="agents-page__grid">
              {[1, 2, 3, 4, 5, 6].map(i => <Skeleton key={i} variant="rect" height={212} />)}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title={isFiltered ? 'No matching agents' : 'No agents listed yet'}
              sub={isFiltered ? 'Try a different tag or search query.' : 'Register the first listing to seed the marketplace.'}
              action={
                <div className="agents-page__empty-actions">
                  {isFiltered && (
                    <Button
                      variant="secondary"
                      onClick={() => { setSearch(''); setActiveTag(ALL) }}
                    >
                      Clear filters
                    </Button>
                  )}
                  <Button variant="primary" icon={<Plus size={14} />} onClick={() => setShowRegister(true)}>
                    Register agent
                  </Button>
                </div>
              }
            />
          ) : (
            <div className="agents-page__grid">
              {filtered.map((agent, index) => (
                <AgentCard key={agent.agent_id} agent={agent} index={index} />
              ))}
            </div>
          )}
        </div>
      </div>

      {showRegister && (
        <RegisterDialog
          apiKey={apiKey}
          onClose={() => setShowRegister(false)}
          onSuccess={() => { setShowRegister(false); refresh() }}
          showToast={showToast}
        />
      )}
    </main>
  )
}
