import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import AgentCard from '../features/agents/AgentCard'
import EmptyState from '../ui/EmptyState'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Pill from '../ui/Pill'
import Skeleton from '../ui/Skeleton'
import Reveal from '../ui/motion/Reveal'
import { searchAgents } from '../api'
import { useMarket } from '../context/MarketContext'
import { Search } from 'lucide-react'
import './AgentsPage.css'

const ALL = '__all__'

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

const KIND_FILTERS = [
  { value: ALL, label: 'All agents' },
  { value: 'aztea_built', label: 'Aztea-built' },
  { value: 'community_skill', label: 'Community skills' },
  { value: 'self_hosted', label: 'Self-hosted' },
]

export default function AgentsPage() {
  const { agents, loading, apiKey, refresh } = useMarket()
  const [search, setSearch] = useState('')
  const [activeTag, setActiveTag] = useState(ALL)
  const [activeKind, setActiveKind] = useState(ALL)
  const [sortBy, setSortBy] = useState('trust')
  const [maxPriceCents, setMaxPriceCents] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchError, setSearchError] = useState('')

  // Instant local text filter - runs synchronously so search feels immediate
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
        const kindParam = activeKind !== ALL ? activeKind : undefined
        const data = await searchAgents(apiKey, query, { kind: kindParam })
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
  }, [apiKey, search, activeKind])

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
      if (activeKind !== ALL && a.kind !== activeKind) return false
      if (maxCents != null && (a.price_per_call_usd ?? 0) * 100 > maxCents) return false
      return true
    })
    if (!search.trim()) list = sortAgents(list, sortBy)
    return list
  }, [agents, search, searchResults, localMatched, activeTag, activeKind, sortBy, maxPriceCents])

  // Featured = Aztea-built agents sorted by trust, shown before others when no filter active
  const featured = useMemo(() => {
    if (search.trim() || activeTag !== ALL || activeKind !== ALL || maxPriceCents) return []
    return agents
      .filter(a => a.kind === 'aztea_built')
      .sort((a, b) => (b.trust_score ?? 0) - (a.trust_score ?? 0))
      .slice(0, 3)
  }, [agents, search, activeTag, maxPriceCents])

  const isFiltered = Boolean(search || activeTag !== ALL || activeKind !== ALL || maxPriceCents)
  const listLoading = loading
  const isSemanticSearching = search.trim() && searchLoading

  const clearFilters = () => { setSearch(''); setActiveTag(ALL); setActiveKind(ALL); setMaxPriceCents('') }

  return (
    <main className="agents-page">
      <Topbar crumbs={[{ label: 'Agents' }]} />

      <div className="agents-page__scroll">
        <div className="agents-page__content">
          <Reveal>
            <header className="agents-page__header">
              <div>
                <p className="agents-page__eyebrow t-micro">Tool catalog</p>
                <h1>Tool catalog</h1>
                <p>
                  Pay-per-call tools for Claude Code. Every tool does something Claude can't do alone — live APIs, real code execution, structured output.
                  {loading ? '' : ` ${agents.length} tool${agents.length !== 1 ? 's' : ''} available.`}
                </p>
              </div>
              <div className="agents-page__header-actions">
                <Link to="/jobs">
                  <Button variant="secondary" size="sm">View jobs</Button>
                </Link>
                <Link to="/list-skill">
                  <Button variant="primary" size="sm">List a skill</Button>
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
                {KIND_FILTERS.map(k => (
                  <Pill key={k.value} interactive active={activeKind === k.value} onClick={() => setActiveKind(k.value)}>
                    {k.label}
                  </Pill>
                ))}
              </div>
              <div className="agents-page__tag-row">
                <Pill interactive active={activeTag === ALL} onClick={() => setActiveTag(ALL)}>All tags</Pill>
                {allTags.map(tag => (
                  <Pill key={tag} interactive active={activeTag === tag} onClick={() => setActiveTag(tag)}>{tag}</Pill>
                ))}
              </div>
            </section>
          </Reveal>

          {listLoading ? (
            <div className="agents-page__grid">
              {[1, 2, 3, 4, 5, 6].map(i => <Skeleton key={i} variant="rect" height={212} />)}
            </div>
          ) : filtered.length === 0 ? (
            <EmptyState
              title={isFiltered ? 'No matching tools' : 'No tools listed yet'}
              sub={isFiltered ? 'Try adjusting your filters or search query.' : 'Be the first to list a skill.'}
              action={
                <div className="agents-page__empty-actions">
                  {isFiltered && <Button variant="secondary" onClick={clearFilters}>Clear filters</Button>}
                  <Link to="/list-skill"><Button variant="primary">List a skill</Button></Link>
                </div>
              }
            />
          ) : (() => {
              const featuredIds = new Set(featured.map(a => a.agent_id))
              const merged = !isFiltered
                ? [...featured, ...filtered.filter(a => !featuredIds.has(a.agent_id))]
                : filtered
              return (
                <>
                  {isFiltered && <p className="agents-page__results-count t-micro">{filtered.length} result{filtered.length !== 1 ? 's' : ''}</p>}
                  <div className="agents-page__grid">
                    {merged.map((agent, index) => (
                      <AgentCard
                        key={agent.agent_id}
                        agent={agent}
                        index={index}
                        featured={featuredIds.has(agent.agent_id)}
                      />
                    ))}
                  </div>
                </>
              )
            })()
          }
        </div>
      </div>
    </main>
  )
}
