import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import AgentCard from '../features/agents/AgentCard'
import EmptyState from '../ui/EmptyState'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Skeleton from '../ui/Skeleton'
import Reveal from '../ui/motion/Reveal'
import GeometricDivider from '../brand/GeometricDivider'
import { searchAgents } from '../api'
import { useMarket } from '../context/MarketContext'
import { Search, Zap } from 'lucide-react'
import './AgentsPage.css'

const SEARCH_SUGGESTIONS = [
  'run Python code', 'audit npm dependencies', 'check SSL certificate',
  'review pull request', 'scan for CVEs', 'browse a webpage',
  'search arXiv papers', 'lint JavaScript', 'execute shell command',
]

const CATEGORIES = [
  { label: 'All', value: '' },
  { label: 'Code', value: 'code' },
  { label: 'Security', value: 'security' },
  { label: 'Web', value: 'web' },
  { label: 'Research', value: 'research' },
  { label: 'Execution', value: 'execution' },
]

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

export default function AgentsPage() {
  const { agents, loading, apiKey, refresh } = useMarket()
  const [searchParams, setSearchParams] = useSearchParams()
  const [filtersOpen, setFiltersOpen] = useState(false)

  const search       = searchParams.get('q') ?? ''
  const sortBy       = searchParams.get('sort') ?? 'trust'
  const maxPriceCents = searchParams.get('max_price') ?? ''
  const category     = searchParams.get('cat') ?? ''

  const setSearch        = (v) => setSearchParams(p => { const n = new URLSearchParams(p); v ? n.set('q', v) : n.delete('q'); return n }, { replace: true })
  const setSortBy        = (v) => setSearchParams(p => { const n = new URLSearchParams(p); v !== 'trust' ? n.set('sort', v) : n.delete('sort'); return n }, { replace: true })
  const setMaxPriceCents = (v) => setSearchParams(p => { const n = new URLSearchParams(p); v ? n.set('max_price', v) : n.delete('max_price'); return n }, { replace: true })
  const setCategory      = (v) => setSearchParams(p => { const n = new URLSearchParams(p); v ? n.set('cat', v) : n.delete('cat'); return n }, { replace: true })

  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')
  const [isNetworkError, setIsNetworkError] = useState(false)
  const [isSemanticResult, setIsSemanticResult] = useState(false)

  // Instant local text filter
  const localMatched = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return agents
    return agents.filter(a =>
      (a.name ?? '').toLowerCase().includes(q) ||
      (a.description ?? '').toLowerCase().includes(q) ||
      (a.tags ?? []).some(t => t.toLowerCase().includes(q))
    )
  }, [agents, search])

  // Debounced semantic search via API
  useEffect(() => {
    const query = search.trim()
    setSearchError('')
    setIsNetworkError(false)
    if (!query) {
      setSearchResults([])
      setSearchLoading(false)
      return
    }

    setSearchLoading(true)
    setIsSemanticResult(false)
    let cancelled = false
    const timer = setTimeout(async () => {
      try {
        const data = await searchAgents(apiKey, query)
        if (cancelled) return
        const normalized = (data?.results ?? []).map(item => {
          const matchReasons = Array.isArray(item?.match_reasons)
            ? item.match_reasons.map(r => (typeof r === 'string' ? r.trim() : '')).filter(Boolean)
            : []
          return { ...(item?.agent ?? {}), match_reasons: matchReasons, _from_search: true }
        })
        setSearchResults(normalized)
        setIsSemanticResult(normalized.length > 0)
      } catch (err) {
        if (cancelled) return
        setSearchResults([])
        setIsNetworkError(true)
        setSearchError(err?.message ?? 'Search failed.')
      } finally {
        if (!cancelled) setSearchLoading(false)
      }
    }, 380)

    return () => { cancelled = true; clearTimeout(timer) }
  }, [apiKey, search])

  const filtered = useMemo(() => {
    const source = search.trim()
      ? (searchResults.length > 0 ? searchResults : localMatched)
      : agents
    const maxCents = maxPriceCents ? parseFloat(maxPriceCents) * 100 : null
    let list = source.filter(a => {
      if (maxCents != null && (a.price_per_call_usd ?? 0) * 100 > maxCents) return false
      if (category && !(a.tags ?? []).some(t => t.toLowerCase() === category)) return false
      return true
    })
    if (!search.trim()) list = sortAgents(list, sortBy)
    return list
  }, [agents, search, searchResults, localMatched, sortBy, maxPriceCents, category])

  // Featured = Aztea-built agents sorted by trust, shown before others when no filter active
  const featured = useMemo(() => {
    if (search.trim() || maxPriceCents || category) return []
    return agents
      .filter(a => a.kind === 'aztea_built')
      .sort((a, b) => (b.trust_score ?? 0) - (a.trust_score ?? 0))
      .slice(0, 3)
  }, [agents, search, maxPriceCents, category])

  const isFiltered = Boolean(search || maxPriceCents || category)
  const listLoading = loading
  const isSemanticSearching = search.trim() && searchLoading

  const clearFilters = () => setSearchParams({}, { replace: true })

  return (
    <main className="agents-page">
      <Topbar crumbs={[{ label: 'Agents' }]} />

      <div className="agents-page__scroll">
        <div className="agents-page__content">
          <Reveal>
            <header className="agents-page__header">
              <div>
                <p className="agents-page__eyebrow t-micro">The catalog</p>
                <h1>Hire a specialist.</h1>
                <p className="agents-page__sub">
                  Each agent does one thing a general model cannot — live APIs, real
                  code execution, fresh data, structured output. Pay per call. Refunds on failure.
                </p>
              </div>
              <div className="agents-page__header-actions">
                <Link to="/jobs">
                  <Button variant="secondary" size="sm">My jobs</Button>
                </Link>
                <Link to="/list-skill">
                  <Button variant="primary" size="sm">List an agent</Button>
                </Link>
              </div>
            </header>
          </Reveal>

          <Reveal delay={0.05}>
            <section className="agents-page__filters">
              <div className="agents-page__filter-row">
                <Input
                  placeholder="Search agents…"
                  value={search}
                  onChange={e => setSearch(e.target.value)}
                  iconLeft={<Search size={14} />}
                  hint={searchError || (isSemanticSearching ? 'Refining with semantic search…' : undefined)}
                />
                <button
                  type="button"
                  className={`agents-page__filter-toggle${filtersOpen || maxPriceCents || sortBy !== 'trust' ? ' agents-page__filter-toggle--active' : ''}`}
                  onClick={() => setFiltersOpen(v => !v)}
                  aria-expanded={filtersOpen}
                >
                  Filters{(maxPriceCents || sortBy !== 'trust') ? ' •' : ''}
                </button>
              </div>
              {filtersOpen && (
                <div className="agents-page__filter-expanded">
                  <Input
                    placeholder="Max price (USD)"
                    type="number"
                    min="0"
                    max="100"
                    step="0.001"
                    value={maxPriceCents}
                    onChange={e => setMaxPriceCents(e.target.value)}
                    className="agents-page__price-input"
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
              )}
              <div className="agents-page__categories" role="group" aria-label="Filter by category">
                {CATEGORIES.map(cat => (
                  <button
                    key={cat.value}
                    className={`agents-page__category-chip${category === cat.value ? ' agents-page__category-chip--active' : ''}`}
                    onClick={() => setCategory(cat.value)}
                  >
                    {cat.label}
                  </button>
                ))}
              </div>
            </section>
          </Reveal>

          {!search && !maxPriceCents && !category && !listLoading && (
            <div className="agents-page__suggestions">
              <span className="agents-page__suggestions-label">Try:</span>
              {SEARCH_SUGGESTIONS.map(s => (
                <button key={s} className="agents-page__suggestion-pill" onClick={() => setSearch(s)}>
                  {s}
                </button>
              ))}
            </div>
          )}

          {listLoading ? (
            <div className="agents-page__grid">
              {[1, 2, 3, 4, 5, 6].map(i => <Skeleton key={i} variant="rect" height={220} />)}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title={isNetworkError ? 'Search unavailable' : isFiltered ? 'No matching specialists' : 'No specialists listed yet'}
              sub={isNetworkError ? 'Could not reach the search service. Try again or browse the full catalog below.' : isFiltered ? 'Try adjusting your filters or search query.' : 'Be the first to list a skill — keep 90% of every successful call.'}
              action={
                <div className="agents-page__empty-actions">
                  {isNetworkError && <Button variant="primary" onClick={() => { setSearch(''); setIsNetworkError(false) }}>Browse all agents</Button>}
                  {!isNetworkError && isFiltered && <Button variant="secondary" onClick={clearFilters}>Clear filters</Button>}
                  {!isNetworkError && <Link to="/list-skill"><Button variant="primary">List an agent</Button></Link>}
                </div>
              }
            />
          ) : isFiltered ? (
            <>
              <div className="agents-page__results-meta">
                <span className="agents-page__results-count t-micro">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</span>
                {isSemanticResult && search.trim() && (
                  <span className="agents-page__semantic-badge">
                    <Zap size={10} />
                    Semantic match
                  </span>
                )}
              </div>
              <div className="agents-page__grid">
                {filtered.map((agent, index) => (
                  <AgentCard key={agent.agent_id} agent={agent} index={index} showTrust={sortBy === 'trust'} />
                ))}
              </div>
            </>
          ) : (
            <>
              {featured.length > 0 && (
                <section>
                  <div className="agents-page__section-header">
                    <span className="agents-page__section-label">Curated by Aztea</span>
                    <GeometricDivider />
                  </div>
                  <div className="agents-page__grid agents-page__grid--featured">
                    {featured.map((agent, index) => (
                      <AgentCard
                        key={agent.agent_id}
                        agent={agent}
                        index={index}
                        featured
                        showTrust={sortBy === 'trust'}
                      />
                    ))}
                  </div>
                </section>
              )}

              {filtered.filter(a => !featured.some(f => f.agent_id === a.agent_id)).length > 0 && (
                <section>
                  <div className="agents-page__section-header">
                    <span className="agents-page__section-label">All agents</span>
                    <GeometricDivider />
                  </div>
                  <div className="agents-page__grid">
                    {filtered
                      .filter(a => !featured.some(f => f.agent_id === a.agent_id))
                      .map((agent, index) => (
                        <AgentCard
                          key={agent.agent_id}
                          agent={agent}
                          index={index + featured.length}
                          showTrust={sortBy === 'trust'}
                        />
                      ))}
                  </div>
                </section>
              )}
            </>
          )}
        </div>
      </div>
    </main>
  )
}
