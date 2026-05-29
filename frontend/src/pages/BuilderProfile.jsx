import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import Card from '../ui/Card'
import Badge from '../ui/Badge'
import EmptyState from '../ui/EmptyState'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import { fetchBuilder } from '../api'
import { ArrowLeft, Shield, BarChart2, Star, DollarSign } from 'lucide-react'
import './BuilderProfile.css'

// Wave 2 (2026-05-26): public builder profile page.
//
// /builders/<username> — surfaces the human behind the published agents:
// agent list, lifetime calls served, average rating, trust score, and
// (only when the builder opted in via users.profile_visible_earnings)
// total earnings. PUBLIC — no API key needed, just like an open
// catalog. Authed callers see the same shape.
//
// `earnings_visible=false` is the "field omitted entirely" signal from
// the backend — we hide the entire Earnings card rather than showing
// "$0" or "—", because zero would be misleading and "—" would imply
// data is missing rather than intentionally private.

function fmtRating(rating) {
  if (rating === null || rating === undefined) return '—'
  return `${rating.toFixed(2)} / 5`
}

function fmtTrust(trust) {
  if (trust === null || trust === undefined) return '—'
  // Trust is stored 0–1 in some snapshots, 0–100 in others.
  // Display in the natural range: if ≤ 1, show as percent; else as-is.
  if (trust <= 1) return `${(trust * 100).toFixed(0)}%`
  return `${Math.round(trust)}%`
}

function fmtUSD(usd) {
  if (usd === null || usd === undefined) return '—'
  return `$${usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function BuilderHeader({ profile }) {
  return (
    <div className="bp-header">
      <div className="bp-header__back">
        <Link to="/agents">
          <ArrowLeft size={14} aria-hidden /> All agents
        </Link>
      </div>
      <h1 className="bp-header__name">{profile.username}</h1>
      <p className="bp-header__sub">
        Builder · {profile.agent_count} agent{profile.agent_count === 1 ? '' : 's'} published
      </p>
    </div>
  )
}

function StatCard({ icon: Icon, label, value, hint }) {
  return (
    <Card padding="md" className="bp-stat">
      <div className="bp-stat__icon"><Icon size={18} aria-hidden /></div>
      <div className="bp-stat__label">{label}</div>
      <div className="bp-stat__value">{value}</div>
      {hint ? <div className="bp-stat__hint">{hint}</div> : null}
    </Card>
  )
}

function AgentListItem({ agent }) {
  const slug = agent.slug || agent.agent_id
  return (
    <Card padding="md" className="bp-agent">
      <Link to={`/agents/${slug}`} className="bp-agent__link">
        <div className="bp-agent__title">{agent.name || slug}</div>
        {agent.description ? (
          <div className="bp-agent__desc">{agent.description}</div>
        ) : null}
        <div className="bp-agent__meta">
          {agent.category ? <Badge>{agent.category}</Badge> : null}
          {agent.price_per_call_usd != null ? (
            <span>${Number(agent.price_per_call_usd).toFixed(2)}/call</span>
          ) : null}
          {agent.total_calls != null ? (
            <span>{agent.total_calls.toLocaleString()} calls</span>
          ) : null}
        </div>
      </Link>
    </Card>
  )
}

export default function BuilderProfile() {
  const { username } = useParams()
  const [profile, setProfile] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    fetchBuilder(username)
      .then((data) => {
        if (!cancelled) setProfile(data)
      })
      .catch((err) => {
        if (!cancelled) {
          // Inline error per the engineering style — toasts are for success only.
          setError(err?.message || 'Could not load builder profile.')
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [username])

  if (loading) {
    return (
      <div className="bp">
        <BuilderHeader profile={{ username, agent_count: 0 }} />
        <p className="bp-loading">Loading…</p>
      </div>
    )
  }

  if (error || !profile) {
    return (
      <div className="bp">
        <BuilderHeader profile={{ username, agent_count: 0 }} />
        <EmptyState
          title="Builder not found"
          body={error || `No public profile for @${username}.`}
          action={<Link to="/agents">Browse all agents</Link>}
        />
      </div>
    )
  }

  return (
    <div className="bp">
      <Reveal>
        <BuilderHeader profile={profile} />
      </Reveal>

      <Stagger className="bp-stats">
        <StatCard
          icon={BarChart2}
          label="Calls served"
          value={profile.total_calls_served.toLocaleString()}
          hint="Lifetime across every published agent"
        />
        <StatCard
          icon={Star}
          label="Average rating"
          value={fmtRating(profile.average_rating)}
          hint={profile.average_rating == null ? 'No ratings yet' : 'From caller feedback'}
        />
        <StatCard
          icon={Shield}
          label="Trust score"
          value={fmtTrust(profile.trust_score)}
          hint="Weighted across all agents"
        />
        {/*
          Earnings card is rendered ONLY when the builder opted in (the
          backend omits the field otherwise). Showing "$0" or "—" would
          be a different signal than "private by design"; hiding the
          entire card is the right call.
        */}
        {profile.earnings_visible && profile.total_earnings_usd != null ? (
          <StatCard
            icon={DollarSign}
            label="Total earnings"
            value={fmtUSD(profile.total_earnings_usd)}
            hint="Lifetime payouts (opted in)"
          />
        ) : null}
      </Stagger>

      <section className="bp-agents">
        <h2 className="bp-section-title">Agents</h2>
        {profile.agents.length === 0 ? (
          <EmptyState
            title="No agents published yet"
            body={`@${profile.username} hasn't shipped any public agents.`}
          />
        ) : (
          <Stagger>
            {profile.agents.map((agent) => (
              <AgentListItem key={agent.agent_id || agent.slug} agent={agent} />
            ))}
          </Stagger>
        )}
      </section>
    </div>
  )
}
