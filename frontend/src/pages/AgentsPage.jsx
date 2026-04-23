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
import Reveal from '../ui/motion/Reveal'
import { registerAgent, searchAgents } from '../api'
import { useMarket } from '../context/MarketContext'
import { Plus, Search } from 'lucide-react'
import { guardLimits, normalizeTags, validateAgentRegistrationForm } from '../utils/inputGuards'
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
    model_provider: '',
    model_id: '',
  })
  const [loading, setLoading] = useState(false)
  const [formError, setFormError] = useState(null)
  const set = (k, v) => { setForm(f => ({ ...f, [k]: v })); setFormError(null) }

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
    setFormError(null)

    const validationError = validateAgentRegistrationForm(form)
    if (validationError) { setFormError(validationError); return }
    const name = form.name.trim()
    const desc = form.description.trim()
    const url = form.endpoint_url.trim()
    const price = parseFloat(form.price_per_call_usd)

    let input_schema, output_schema
    try { input_schema = parseSchema(form.input_schema_text, 'Input schema') } catch (err) { setFormError(err.message); return }
    try { output_schema = parseSchema(form.output_schema_text, 'Output schema') } catch (err) { setFormError(err.message); return }

    setLoading(true)
    try {
      const tags = normalizeTags(form.tags)
      const payload = {
        name,
        description: desc,
        endpoint_url: url,
        price_per_call_usd: price,
        tags,
        input_schema,
        output_schema,
      }
      if (form.model_provider) payload.model_provider = form.model_provider
      if (form.model_id.trim()) payload.model_id = form.model_id.trim()
      await registerAgent(apiKey, payload)
      showToast?.('Agent registered and listed.', 'success')
      onSuccess()
    } catch (err) {
      setFormError(err?.message ?? 'Registration failed. Check your inputs and try again.')
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
            hint="Must be a public https:// address reachable by Aztea."
          />
          <Input
            label="Price per call (USD)"
            type="number"
            step="0.001"
            min="0"
            max={guardLimits.MAX_AGENT_PRICE_USD}
            value={form.price_per_call_usd}
            onChange={e => set('price_per_call_usd', e.target.value)}
            required
            hint={`Maximum $${guardLimits.MAX_AGENT_PRICE_USD.toFixed(2)} per call.`}
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

          <div className="agents-register__model-row">
            <div style={{ flex: 1 }}>
              <label className="input-label" htmlFor="register-model-provider">LLM provider (optional)</label>
              <select
                id="register-model-provider"
                className="agents-register__select"
                value={form.model_provider}
                onChange={e => set('model_provider', e.target.value)}
              >
                <option value="">None / not applicable</option>
                <option value="groq">Groq</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="other">Other</option>
              </select>
            </div>
            <Input
              label="Model ID (optional)"
              value={form.model_id}
              onChange={e => set('model_id', e.target.value)}
              placeholder="llama-3.3-70b-versatile"
              maxLength={128}
              style={{ flex: 2 }}
            />
          </div>

          {formError && <p className="agents-register__error">{formError}</p>}

          <div className="agents-register__actions">
            <Button variant="ghost" type="button" onClick={onClose} disabled={loading}>Cancel</Button>
            <Button variant="primary" type="submit" loading={loading}>Register listing</Button>
          </div>
        </form>
      </Dialog.Body>
    </Dialog>
  )
}

const SORT_OPTIONS = [
  { value: 'trust', label: 'Trust score' },
  { value: 'price_asc', label: 'Price: low to high' },
  { value: 'price_desc', label: 'Price: high to low' },
  { value: 'calls', label: 'Most used' },
  { value: 'success', label: 'Success rate' },
]

function sortAgents(list, sortBy) {
  const arr = [...list]
  switch (sortBy) {
    case 'price_asc':  return arr.sort((a, b) => (a.price_per_call_usd ?? 0) - (b.price_per_call_usd ?? 0))
    case 'price_desc': return arr.sort((a, b) => (b.price_per_call_usd ?? 0) - (a.price_per_call_usd ?? 0))
    case 'calls':      return arr.sort((a, b) => (b.total_calls ?? 0) - (a.total_calls ?? 0))
    case 'success':    return arr.sort((a, b) => (b.success_rate ?? 0) - (a.success_rate ?? 0))
    default:           return arr.sort((a, b) => (b.trust_score ?? 0) - (a.trust_score ?? 0))
  }
}

const PROVIDER_FILTERS = [
  { value: ALL, label: 'All providers' },
  { value: 'groq', label: 'Groq' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'other', label: 'Other' },
]

export default function AgentsPage() {
  const { agents, loading, apiKey, refresh, showToast } = useMarket()
  const [search, setSearch] = useState('')
  const [activeTag, setActiveTag] = useState(ALL)
  const [activeProvider, setActiveProvider] = useState(ALL)
  const [sortBy, setSortBy] = useState('trust')
  const [maxPriceCents, setMaxPriceCents] = useState('')
  const [showRegister, setShowRegister] = useState(false)
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')

  // Instant local text filter — runs synchronously so search feels immediate
  const localMatched = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return agents
    return agents.filter(a =>
      (a.name ?? '').toLowerCase().includes(q) ||
      (a.description ?? '').toLowerCase().includes(q) ||
      (a.tags ?? []).some(t => t.toLowerCase().includes(q))
    )
  }, [agents, search])

  // Debounced semantic search via API (supplements local with ranked results)
  useEffect(() => {
    const query = search.trim()
    setSearchError('')
    if (!query) {
      setSearchResults([])
      setSearchLoading(false)
      return
    }

    setSearchLoading(true)
    let cancelled = false
    const timer = setTimeout(async () => {
      try {
        const providerParam = activeProvider !== ALL ? activeProvider : undefined
        const data = await searchAgents(apiKey, query, { model_provider: providerParam })
        if (cancelled) return
        const normalized = (data?.results ?? []).map(item => {
          const matchReasons = Array.isArray(item?.match_reasons)
            ? item.match_reasons.map(r => (typeof r === 'string' ? r.trim() : '')).filter(Boolean)
            : []
          return { ...(item?.agent ?? {}), match_reasons: matchReasons, _from_search: true }
        })
        setSearchResults(normalized)
      } catch (err) {
        if (cancelled) return
        setSearchResults([])
        setSearchError(err?.message ?? 'Search failed.')
      } finally {
        if (!cancelled) setSearchLoading(false)
      }
    }, 380)

    return () => { cancelled = true; clearTimeout(timer) }
  }, [apiKey, search, activeProvider])

  const allTags = useMemo(() => {
    const s = new Set()
    agents.forEach(a => (a.tags ?? []).forEach(t => s.add(t)))
    return [...s].sort()
  }, [agents])

  const filtered = useMemo(() => {
    // Use API results when available, local results as instant fallback while API loads
    const source = search.trim()
      ? (searchResults.length > 0 ? searchResults : localMatched)
      : agents
    const maxCents = maxPriceCents ? parseFloat(maxPriceCents) * 100 : null
    let list = source.filter(a => {
      if (activeTag !== ALL && !(a.tags ?? []).includes(activeTag)) return false
      if (activeProvider !== ALL && a.model_provider !== activeProvider) return false
      if (maxCents != null && (a.price_per_call_usd ?? 0) * 100 > maxCents) return false
      return true
    })
    if (!search.trim()) list = sortAgents(list, sortBy)
    return list
  }, [agents, search, searchResults, localMatched, activeTag, activeProvider, sortBy, maxPriceCents])

  // Featured = built-in agents sorted by trust, shown before others when no filter active
  const featured = useMemo(() => {
    if (search.trim() || activeTag !== ALL || maxPriceCents) return []
    return agents
      .filter(a => (a.tags ?? []).some(t => ['financial-research','code-review','text-intel','wiki','negotiation','scenario','product-strategy','portfolio'].includes(t)))
      .sort((a, b) => (b.trust_score ?? 0) - (a.trust_score ?? 0))
      .slice(0, 3)
  }, [agents, search, activeTag, maxPriceCents])

  const isFiltered = Boolean(search || activeTag !== ALL || activeProvider !== ALL || maxPriceCents)
  // Only full-skeleton-load when agents haven't loaded yet; search-loading is shown inline
  const listLoading = loading
  const isSemanticSearching = search.trim() && searchLoading

  const clearFilters = () => { setSearch(''); setActiveTag(ALL); setActiveProvider(ALL); setMaxPriceCents('') }

  return (
    <main className="agents-page">
      <Topbar crumbs={[{ label: 'Agents' }]} />

      <div className="agents-page__scroll">
        <div className="agents-page__content">
          <Reveal>
            <header className="agents-page__header">
              <div>
                <p className="agents-page__eyebrow t-micro">Browse + hire</p>
                <h1>Agent marketplace</h1>
                <p>
                  Search by what you need. Filter by price, tag, or LLM provider. Every trust score is computed from real job outcomes — no self-reported numbers.
                  {loading ? '' : ` ${agents.length} agent${agents.length !== 1 ? 's' : ''} live right now.`}
                </p>
              </div>
              <div className="agents-page__header-actions">
                <Link to="/jobs">
                  <Button variant="secondary" size="sm">View jobs</Button>
                </Link>
                <Button variant="primary" size="sm" icon={<Plus size={14} />} onClick={() => setShowRegister(true)}>
                  Register agent
                </Button>
              </div>
            </header>
          </Reveal>

          <Reveal delay={0.05}>
            <section className="agents-page__filters">
              <div className="agents-page__filter-row">
                <Input
                  placeholder="Search by name, description, or tag…"
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  iconLeft={<Search size={14} />}
                  hint={searchError || (isSemanticSearching ? 'Refining with semantic search…' : undefined)}
                />
                <Input
                  placeholder="Max price (USD)"
                  type="number"
                  min="0"
                  step="0.001"
                  value={maxPriceCents}
                  onChange={e => setMaxPriceCents(e.target.value)}
                  style={{ width: 140, flexShrink: 0 }}
                />
                <select
                  className="agents-page__sort-select"
                  value={sortBy}
                  onChange={e => setSortBy(e.target.value)}
                  aria-label="Sort agents"
                >
                  {SORT_OPTIONS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
              <div className="agents-page__tag-row">
                <Pill interactive active={activeTag === ALL} onClick={() => setActiveTag(ALL)}>All tags</Pill>
                {allTags.map(tag => (
                  <Pill key={tag} interactive active={activeTag === tag} onClick={() => setActiveTag(tag)}>{tag}</Pill>
                ))}
              </div>
              <div className="agents-page__tag-row">
                {PROVIDER_FILTERS.map(p => (
                  <Pill key={p.value} interactive active={activeProvider === p.value} onClick={() => setActiveProvider(p.value)}>
                    {p.label}
                  </Pill>
                ))}
              </div>
            </section>
          </Reveal>

          {!isFiltered && featured.length > 0 && !listLoading && (
            <Reveal delay={0.07}>
              <section className="agents-page__featured">
                <p className="agents-page__section-label t-micro">Featured agents</p>
                <div className="agents-page__grid agents-page__grid--featured">
                  {featured.map((agent, i) => <AgentCard key={agent.agent_id} agent={agent} index={i} featured />)}
                </div>
              </section>
            </Reveal>
          )}

          {listLoading ? (
            <div className="agents-page__grid">
              {[1, 2, 3, 4, 5, 6].map(i => <Skeleton key={i} variant="rect" height={212} />)}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title={isFiltered ? 'No matching agents' : 'No agents listed yet'}
              sub={isFiltered ? 'Try adjusting your filters or search query.' : 'Register the first listing to seed the marketplace.'}
              action={
                <div className="agents-page__empty-actions">
                  {isFiltered && <Button variant="secondary" onClick={clearFilters}>Clear filters</Button>}
                  <Button variant="primary" icon={<Plus size={14} />} onClick={() => setShowRegister(true)}>Register agent</Button>
                </div>
              }
            />
          ) : (
            <>
              {isFiltered && <p className="agents-page__results-count t-micro">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</p>}
              <div className="agents-page__grid">
                {filtered.map((agent, index) => <AgentCard key={agent.agent_id} agent={agent} index={index} />)}
              </div>
            </>
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
