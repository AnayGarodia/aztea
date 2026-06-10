// OWNS: the owner-facing "Suggested improvements" panel for one hosted skill.
//   Lists proposed learnings (distilled from the skill's recent failures) and
//   lets the owner accept (inject at run time) or reject (dismiss) each one.
// NOT OWNS: the distiller (core/observability.py) or the store
//   (core/skill_learnings.py). Talks only through src/api.js.
// NOTE: renders nothing when the feature is off (the GET 404s), when the skill
//   has no proposals, or before the first load resolves — so it is safe to mount
//   unconditionally for any hosted skill. Errors are shown inline (never toasts),
//   per the project's frontend error rule.
import { useState, useEffect, useCallback } from 'react'
import { Lightbulb, Check, X } from 'lucide-react'
import Button from '../../ui/Button'
import { fetchSkillLearnings, decideSkillLearning } from '../../api'

export default function SkillLearningsPanel({ apiKey, skillId }) {
  const [learnings, setLearnings] = useState([])
  const [loaded, setLoaded] = useState(false)
  const [busyId, setBusyId] = useState(null)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    if (!apiKey || !skillId) return
    try {
      const data = await fetchSkillLearnings(apiKey, skillId, 'proposed')
      setLearnings(Array.isArray(data?.learnings) ? data.learnings : [])
    } catch {
      // Feature disabled (404) or not visible — render nothing, no error noise.
      setLearnings([])
    } finally {
      setLoaded(true)
    }
  }, [apiKey, skillId])

  useEffect(() => { load() }, [load])

  const decide = async (learningId, decision) => {
    setBusyId(learningId)
    setError('')
    try {
      await decideSkillLearning(apiKey, skillId, learningId, decision)
      // Optimistically drop the decided item from the pending list.
      setLearnings(prev => prev.filter(l => l.learning_id !== learningId))
    } catch (err) {
      setError(err?.message || 'Could not save your decision. Try again.')
    } finally {
      setBusyId(null)
    }
  }

  if (!loaded || learnings.length === 0) return null

  return (
    <div
      style={{
        marginTop: 'var(--sp-3)',
        padding: 'var(--sp-3)',
        border: '1px solid var(--border)',
        borderRadius: 'var(--r-sm)',
        background: 'var(--surface-2)',
      }}
    >
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 'var(--sp-2)',
          fontSize: '0.8125rem', fontWeight: 600, marginBottom: 'var(--sp-1)',
        }}
      >
        <Lightbulb size={13} color="var(--accent)" />
        <span>Suggested improvements ({learnings.length})</span>
      </div>
      <p style={{ fontSize: '0.75rem', color: 'var(--ink-soft)', lineHeight: 1.5, margin: '0 0 var(--sp-2)' }}>
        Distilled from this skill's recent low-rated runs and disputes. Accepted
        suggestions are injected into the skill at run time. You can dismiss them
        anytime by deleting the skill or rejecting them here.
      </p>
      {error && (
        <p style={{ fontSize: '0.75rem', color: 'var(--negative)', margin: '0 0 var(--sp-2)' }}>
          {error}
        </p>
      )}
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 'var(--sp-2)' }}>
        {learnings.map(l => (
          <li
            key={l.learning_id}
            style={{
              display: 'flex', alignItems: 'flex-start', gap: 'var(--sp-2)',
              justifyContent: 'space-between',
            }}
          >
            <span style={{ flex: 1 }}>
              <span style={{ fontSize: '0.8125rem', lineHeight: 1.45 }}>{l.text}</span>
              {l.source_signal && (
                <span style={{ display: 'block', fontSize: '0.6875rem', color: 'var(--ink-soft)', marginTop: '2px' }}>
                  from {l.source_signal === 'dispute' ? 'a dispute' : l.source_signal === 'example' ? 'a low-rated run' : l.source_signal}
                </span>
              )}
            </span>
            <div style={{ display: 'flex', gap: 'var(--sp-1)', flexShrink: 0 }}>
              <Button
                size="sm"
                variant="primary"
                icon={<Check size={12} />}
                loading={busyId === l.learning_id}
                onClick={() => decide(l.learning_id, 'accept')}
              >
                Accept
              </Button>
              <Button
                size="sm"
                variant="ghost"
                icon={<X size={12} />}
                disabled={busyId === l.learning_id}
                onClick={() => decide(l.learning_id, 'reject')}
              >
                Reject
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
