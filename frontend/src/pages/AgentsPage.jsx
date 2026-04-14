import Topbar from '../layout/Topbar'
import EmptyState from '../ui/EmptyState'
import AgentCard from '../features/agents/AgentCard'
import { useMarket } from '../context/MarketContext'

export default function AgentsPage() {
  const { agents, loading } = useMarket()

  return (
    <main style={{ padding: 24, display: 'grid', gap: 16 }}>
      <Topbar crumbs={[{ label: 'Agents' }]} />
      {loading ? (
        <p style={{ color: 'var(--ink-mute)' }}>Loading agents…</p>
      ) : agents.length === 0 ? (
        <EmptyState title="No agents found" sub="Register an agent to get started." />
      ) : (
        <section style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
          {agents.map((agent, index) => (
            <AgentCard key={agent.agent_id} agent={agent} index={index} />
          ))}
        </section>
      )}
    </main>
  )
}
