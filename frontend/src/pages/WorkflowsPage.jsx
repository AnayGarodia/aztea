// OWNS: discoverability of platform-curated multi-step recipes (the
// audit-deps / domain-health catalog). Surfaces each recipe's steps,
// slug, total cost, and lets the user launch a run with the standard
// AgentInputForm pre-bound to the recipe's default_input_schema.
//
// NOT OWNS: the run-detail page (existing /jobs/:id), the pipeline
// executor (core/pipelines/), or per-step monitoring beyond launch.
//
// INVARIANTS: fetchRecipes is the single source of truth — never
// hardcode the recipe catalog client-side. The page renders whatever the
// server returns, including any new recipes added later.
//
// DECISIONS: cached in component state for the page's lifetime; recipes
// don't change at runtime, so a re-fetch only happens on page remount.
//
// KNOWN DEBT: clicking Run on a recipe with a missing_agents entry
// allows submission and lets the server return its existing 404 / refund
// path. Surfacing the gap pre-submit would need a richer card state.

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Workflow } from 'lucide-react'
import Topbar from '../layout/Topbar'
import Button from '../ui/Button'
import Card from '../ui/Card'
import Pill from '../ui/Pill'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import Dialog from '../ui/Dialog'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import AgentInputForm from '../features/agents/AgentInputForm'
import { fetchRecipes, runRecipe } from '../api'
import { useAuth } from '../context/AuthContext'
import { formatApiError } from '../utils/errorCopy.js'
import { fmtUsd } from '../utils/format.js'
import './WorkflowsPage.css'

function recipeToAgentShape(recipe) {
  // AgentInputForm is keyed by agent.agent_id (re-derives fields on change)
  // and reads agent.input_schema. Map a recipe into that shape so we don't
  // fork the form component.
  return {
    agent_id: recipe.slug,
    name: recipe.name,
    input_schema: recipe.default_input_schema || { type: 'object' },
  }
}

function StepPill({ step }) {
  // A step with no agent_slug means the agent was removed from the catalog
  // after this recipe was authored; surface it visually so the user can
  // see *why* the cost might be lower than expected.
  const missing = !step.agent_slug
  const label = step.agent_slug || step.agent_id || 'missing agent'
  return (
    <Pill className={missing ? 'workflows__step-pill workflows__step-pill--missing' : 'workflows__step-pill'}>
      <span className="workflows__step-role t-micro">{step.role}</span>
      <span className="workflows__step-slug t-mono">{label}</span>
    </Pill>
  )
}

function RecipeCard({ recipe, onRun }) {
  const totalCents = Math.round((Number(recipe.estimated_total_cost_usd) || 0) * 100)
  return (
    <Card className="workflows__card">
      <Card.Header>
        <div className="workflows__card-head">
          <div>
            <h2 className="workflows__card-name">{recipe.name}</h2>
            <p className="workflows__card-slug t-mono">{recipe.slug}</p>
          </div>
          <span className="workflows__card-cost t-mono" title="Estimated total cost across all steps">
            {fmtUsd(totalCents)}
          </span>
        </div>
      </Card.Header>
      <Card.Body>
        <p className="workflows__card-desc">{recipe.description}</p>
        <div className="workflows__steps">
          {recipe.steps?.map((step, idx) => (
            <StepPill key={step.node_id || `${step.agent_id}-${idx}`} step={step} />
          ))}
        </div>
        {recipe.missing_agents && recipe.missing_agents.length > 0 && (
          <p className="workflows__warning t-micro">
            {recipe.missing_agents.length} step
            {recipe.missing_agents.length === 1 ? '' : 's'} reference an agent that’s no longer
            in the catalog. The run will skip and refund the missing steps.
          </p>
        )}
      </Card.Body>
      <Card.Footer>
        <Button variant="primary" size="sm" onClick={() => onRun(recipe)}>
          Run workflow
        </Button>
      </Card.Footer>
    </Card>
  )
}

export default function WorkflowsPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()
  const [recipes, setRecipes] = useState(null) // null = loading, [] = loaded empty
  const [fetchError, setFetchError] = useState(null)
  const [activeRecipe, setActiveRecipe] = useState(null)
  const [runError, setRunError] = useState(null)
  const [running, setRunning] = useState(false)

  useEffect(() => {
    let cancelled = false
    if (!apiKey) return undefined
    setRecipes(null)
    setFetchError(null)
    fetchRecipes(apiKey)
      .then(body => {
        if (cancelled) return
        setRecipes(Array.isArray(body?.recipes) ? body.recipes : [])
      })
      .catch(err => {
        if (cancelled) return
        setFetchError(formatApiError(err, { action: 'load workflows' }))
      })
    return () => { cancelled = true }
  }, [apiKey])

  const recipeAgent = useMemo(
    () => (activeRecipe ? recipeToAgentShape(activeRecipe) : null),
    [activeRecipe]
  )

  // AgentInputForm calls onSubmit(payload, { privateTask }). The privateTask
  // flag is hire-side bookkeeping that doesn't apply to recipe runs (recipes
  // are billed as one parent transaction by the server, not per-step), so
  // we ignore it.
  const handleRunSubmit = async (payload) => {
    if (!activeRecipe) return
    setRunning(true)
    setRunError(null)
    try {
      await runRecipe(apiKey, activeRecipe.slug, payload)
      // Recipe runs surface under /jobs once their sub-jobs spawn — send
      // the user there so they can watch progress rather than dropping
      // them on a blank success state.
      setActiveRecipe(null)
      navigate('/jobs')
    } catch (err) {
      setRunError(formatApiError(err, { action: `run ${activeRecipe.slug}` }))
    } finally {
      setRunning(false)
    }
  }

  const loading = recipes === null && !fetchError

  return (
    <main className="workflows">
      <Topbar crumbs={[{ label: 'Workflows' }]} />

      <div className="workflows__scroll">
        <div className="workflows__content">
          <Reveal>
            <header className="workflows__header">
              <div>
                <p className="workflows__eyebrow t-micro">Multi-step recipes</p>
                <h1>Workflows</h1>
                <p className="workflows__lede">
                  Curated chains of specialist agents that run as a single hire. One input, one
                  receipt, one settled charge.
                </p>
              </div>
              <span className="workflows__icon" aria-hidden>
                <Workflow size={28} />
              </span>
            </header>
          </Reveal>

          {fetchError && (
            <Card className="workflows__error">
              <Card.Body>
                <p className="workflows__error-title">{fetchError.title}</p>
                {fetchError.hint && (
                  <p className="workflows__error-hint t-micro">{fetchError.hint}</p>
                )}
              </Card.Body>
            </Card>
          )}

          {loading && (
            <div className="workflows__grid">
              {[1, 2, 3].map(i => (
                <Skeleton key={i} variant="rect" height={220} />
              ))}
            </div>
          )}

          {!loading && !fetchError && recipes && recipes.length === 0 && (
            <EmptyState
              title="No workflows configured yet"
              sub="Built-in recipes haven’t been seeded on this server. Once a recipe is registered, it appears here."
            />
          )}

          {!loading && recipes && recipes.length > 0 && (
            <Stagger className="workflows__grid">
              {recipes.map(recipe => (
                <RecipeCard
                  key={recipe.slug}
                  recipe={recipe}
                  onRun={r => { setRunError(null); setActiveRecipe(r) }}
                />
              ))}
            </Stagger>
          )}
        </div>
      </div>

      <Dialog
        open={Boolean(activeRecipe)}
        onClose={() => { if (!running) setActiveRecipe(null) }}
        title={activeRecipe ? `Run ${activeRecipe.name}` : 'Run workflow'}
        size="md"
      >
        {activeRecipe && (
          <div className="workflows__dialog-body">
            <p className="workflows__dialog-desc">{activeRecipe.description}</p>
            {runError && (
              <div className="workflows__dialog-error" role="alert">
                <p>{runError.title}</p>
                {runError.hint && <p className="t-micro">{runError.hint}</p>}
              </div>
            )}
            <AgentInputForm
              agent={recipeAgent}
              loading={running}
              onSubmit={handleRunSubmit}
              mode="async"
              onModeChange={() => {}}
            />
          </div>
        )}
      </Dialog>
    </main>
  )
}
