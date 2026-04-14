import { useState, useMemo } from 'react'
import Topbar from '../layout/Topbar'
import AgentCard from '../features/agents/AgentCard'
import EmptyState from '../ui/EmptyState'
import Button from '../ui/Button'
import Input from '../ui/Input'
import Pill from '../ui/Pill'
import Dialog from '../ui/Dialog'
import Skeleton from '../ui/Skeleton'
import { registerAgent } from '../api'
import { useMarket } from '../context/MarketContext'
import { Plus, Search } from 'lucide-react'

const ALL = '__all__'

function RegisterDialog({ apiKey, onClose, onSuccess, showToast }) {
  const [form, setForm] = useState({
    name: '', description: '', endpoint_url: '',
    price_per_call_usd: '0.01', tags: '',
  })
  const [loading, setLoading] = useState(false)
  const set = (k, v) => setForm(f => ({ ...f, [k]: v }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const tags = form.tags.split(',').map(t => t.trim()).filter(Boolean)
      await registerAgent(apiKey, {
        name: form.name.trim(),
        description: form.description.trim(),
        endpoint_url: form.endpoint_url.trim(),
        price_per_call_usd: parseFloat(form.price_per_call_usd) || 0.01,
        tags,
      })
      showToast?.('Agent registered.', 'success')
      onSuccess()
    } catch (err) {
      showToast?.(err?.message ?? 'Registration failed.', 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Dialog open title="Register agent" onClose={onClose}>
      <Dialog.Body>
        <form onSubmit={handleSubmit} id="register-form" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-4)' }}>
          <Input
            label="Name"
            value={form.name}
            onChange={e => set('name', e.target.value)}
            required
            placeholder="My Research Agent"
          />
          <Input
            label="Description"
            value={form.description}
            onChange={e => set('description', e.target.value)}
            placeholder="What this agent does in one sentence"
          />
          <Input
            label="Endpoint URL"
            type="url"
            value={form.endpoint_url}
            onChange={e => set('endpoint_url', e.target.value)}
            required
            placeholder="https://my-agent.example.com/invoke"
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
            placeholder="nlp, financial-research, code-review"
            hint="Comma-separated. Used for discovery."
          />
          <div style={{ display: 'flex', gap: 'var(--sp-3)', justifyContent: 'flex-end', paddingTop: 'var(--sp-2)' }}>
            <Button variant="ghost" type="button" onClick={onClose}>Cancel</Button>
            <Button variant="primary" type="submit" loading={loading}>Register</Button>
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

  const allTags = useMemo(() => {
    const s = new Set()
    agents.forEach(a => (a.tags ?? []).forEach(t => s.add(t)))
    return [...s].sort()
  }, [agents])

  const filtered = useMemo(() => {
    const q = search.toLowerCase()
    return agents.filter(a => {
      const matchSearch = !q ||
        a.name.toLowerCase().includes(q) ||
        (a.description ?? '').toLowerCase().includes(q) ||
        (a.tags ?? []).some(t => t.toLowerCase().includes(q))
      const matchTag = activeTag === ALL || (a.tags ?? []).includes(activeTag)
      return matchSearch && matchTag
    })
  }, [agents, search, activeTag])

  const isFiltered = search || activeTag !== ALL

  return (
    <main style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }}>
      <Topbar crumbs={[{ label: 'Agents' }]} />

      <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--sp-6)' }}>

        {/* Header */}
        <div style={{
          display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between',
          gap: 'var(--sp-4)', marginBottom: 'var(--sp-5)', flexWrap: 'wrap',
        }}>
          <div>
            <h1 style={{
              fontFamily: 'var(--font-display)',
              fontSize: '1.625rem',
              fontWeight: 400,
              color: 'var(--ink)',
              letterSpacing: '-0.02em',
              lineHeight: 1.2,
              marginBottom: 4,
            }}>
              Agent marketplace
            </h1>
            <p style={{ fontSize: '0.875rem', color: 'var(--ink-mute)' }}>
              {loading ? '…' : `${agents.length} agent${agents.length !== 1 ? 's' : ''} available`}
            </p>
          </div>
          <Button
            variant="primary"
            size="sm"
            icon={<Plus size={14} />}
            onClick={() => setShowRegister(true)}
          >
            Register agent
          </Button>
        </div>

        {/* Search + tag filters */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sp-3)', marginBottom: 'var(--sp-5)' }}>
          <Input
            placeholder="Search by name, description, or tag…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            iconLeft={<Search size={14} />}
          />
          {allTags.length > 0 && (
            <div style={{ display: 'flex', gap: 'var(--sp-2)', flexWrap: 'wrap' }}>
              <Pill interactive active={activeTag === ALL} onClick={() => setActiveTag(ALL)}>
                All
              </Pill>
              {allTags.map(tag => (
                <Pill key={tag} interactive active={activeTag === tag} onClick={() => setActiveTag(tag)}>
                  {tag}
                </Pill>
              ))}
            </div>
          )}
        </div>

        {/* Grid */}
        {loading ? (
          <div style={{ display: 'grid', gap: 'var(--sp-4)', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
            {[1, 2, 3, 4, 5, 6].map(i => <Skeleton key={i} variant="rect" height={180} />)}
          </div>
        ) : filtered.length === 0 ? (
          <EmptyState
            title={isFiltered ? 'No agents match' : 'No agents yet'}
            sub={isFiltered ? 'Try a different search or filter.' : 'Register the first agent to get started.'}
            action={!isFiltered && (
              <Button variant="secondary" icon={<Plus size={14} />} onClick={() => setShowRegister(true)}>
                Register agent
              </Button>
            )}
          />
        ) : (
          <div style={{ display: 'grid', gap: 'var(--sp-4)', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
            {filtered.map((agent, index) => (
              <AgentCard key={agent.agent_id} agent={agent} index={index} />
            ))}
          </div>
        )}
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
