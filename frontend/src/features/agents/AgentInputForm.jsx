import { useState, useMemo, useEffect, useRef, useCallback } from 'react'
import { motion, AnimatePresence } from 'motion/react'
import Button from '../../ui/Button'
import Segmented from '../../ui/Segmented'
import { Zap, Radio, Lock, Unlock } from 'lucide-react'
import { validateInvokePayload } from '../../utils/inputGuards'
import './AgentInputForm.css'

const MODE_OPTIONS = [
  { value: 'sync',  label: 'Sync' },
  { value: 'async', label: 'Async' },
]

function estimateCost(variablePricing, payload) {
  if (!variablePricing) return null
  const { model, field, field_type, tiers, rate_usd, min_usd } = variablePricing

  const raw = payload?.[field]
  if (raw == null || raw === '') return null

  let units
  if (field_type === 'array') {
    if (Array.isArray(raw)) {
      units = raw.filter(Boolean).length
    } else {
      units = String(raw).split(/[\n,]+/).map(s => s.trim()).filter(Boolean).length
    }
  } else {
    units = parseInt(raw, 10)
    if (isNaN(units) || units <= 0) return null
  }

  if (units <= 0) return null

  if (model === 'tiered') {
    const tier = tiers.find(t => units <= t.max_units) ?? tiers[tiers.length - 1]
    return { cost: tier.price_usd, units }
  } else if (model === 'per_unit') {
    return { cost: Math.max(min_usd ?? 0, units * (rate_usd ?? 0)), units }
  }
  return null
}

// Support both legacy {fields:[]} format and standard JSON Schema {properties:{}}
function deriveFields(schema) {
  if (Array.isArray(schema?.fields) && schema.fields.length > 0) return schema.fields
  if (!schema?.properties) return []
  const required = new Set(schema.required ?? [])
  return Object.entries(schema.properties).map(([name, def]) => {
    const label = name.charAt(0).toUpperCase() + name.slice(1).replace(/_/g, ' ')
    let type = 'text'
    if (def.enum) type = 'select'
    else if (['code', 'text', 'content', 'body', 'source'].includes(name)) type = 'textarea'
    return {
      name, label, type,
      options: def.enum,
      required: required.has(name),
      placeholder: def.example ?? def.examples?.[0] ?? '',
      hint: def.description,
      transform: ['ticker', 'symbol'].includes(name) ? 'uppercase' : undefined,
      default: def.default ?? '',
      max_length: def.maxLength,
    }
  })
}

const STEP_VARIANTS = {
  initial: { opacity: 0, y: 18 },
  animate: { opacity: 1, y: 0 },
  exit:    { opacity: 0, y: -18 },
}

const STEP_TRANSITION = { duration: 0.28, ease: [0.16, 1, 0.3, 1] }

export default function AgentInputForm({ agent, onSubmit, loading, mode, onModeChange }) {
  const fields = useMemo(() => deriveFields(agent?.input_schema), [agent])
  const total  = fields.length

  const [step, setStep] = useState(0)
  const [values, setValues] = useState(() =>
    Object.fromEntries(fields.map(f => [f.name, f.default ?? '']))
  )
  const [privateTask, setPrivateTask] = useState(false)
  const [inputError, setInputError] = useState('')

  const inputRef = useRef(null)

  // Reset only when the actual agent changes, not on every parent re-render
  useEffect(() => {
    setValues(Object.fromEntries(fields.map(f => [f.name, f.default ?? ''])))
    setStep(0)
  }, [agent?.agent_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-focus current field
  useEffect(() => {
    const timer = setTimeout(() => inputRef.current?.focus(), 60)
    return () => clearTimeout(timer)
  }, [step])

  const set = (name, val) => setValues(v => ({ ...v, [name]: val }))

  const goNext = useCallback(() => {
    if (step < total - 1) setStep(s => s + 1)
  }, [step, total])

  const goBack = useCallback(() => {
    if (step > 0) setStep(s => s - 1)
  }, [step])

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      const f = fields[step]
      if (f?.type !== 'textarea') {
        e.preventDefault()
        goNext()
      }
    }
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    setInputError('')
    // Validate required fields are non-empty after trimming
    for (const f of fields) {
      if (f.required && !String(values[f.name] ?? '').trim()) {
        // Scroll the user to the offending field
        const idx = fields.indexOf(f)
        setStep(idx)
        setInputError(`"${f.label ?? f.name}" is required. Fill it in before running this agent.`)
        return
      }
    }
    const payload = {}
    fields.forEach(f => {
      let v = values[f.name] ?? ''
      if (f.transform === 'uppercase') v = String(v).toUpperCase()
      payload[f.name] = v
    })
    const payloadError = validateInvokePayload(payload)
    if (payloadError) {
      setInputError(payloadError)
      return
    }
    onSubmit(payload, { privateTask })
  }

  const basePrice = `$${Number(agent?.price_per_call_usd ?? 0).toFixed(2)}`
  const estimatedCost = estimateCost(agent?.variable_pricing, values)
  const price = estimatedCost != null
    ? `$${Number(estimatedCost.cost).toFixed(2)}`
    : basePrice
  const progressPct = total > 0 ? (step / total) * 100 : 0

  // No schema fallback
  if (fields.length === 0) {
    return (
      <form className="invoke-panel" onSubmit={handleSubmit}>
        <p className="invoke-panel__no-schema">
          This agent has no defined input schema. Check its documentation.
        </p>
        <div className="invoke-panel__footer">
          <Segmented options={MODE_OPTIONS} value={mode} onChange={onModeChange} />
          <p className="invoke-panel__mode-help">
            {mode === 'async'
              ? 'Async queues a job you can monitor in Jobs.'
              : 'Sync returns output immediately in this panel.'}
          </p>
          <div className="invoke-panel__price-bar">
            <span className="invoke-panel__price-label">
              {estimatedCost != null
                ? 'Estimated cost'
                : agent?.variable_pricing
                  ? 'Price varies by usage'
                  : 'Cost per call'}
            </span>
            <span className="invoke-panel__price-val">
              {price}
              {estimatedCost != null && agent?.variable_pricing?.unit_label && (
                <span className="invoke-panel__price-hint">
                  {' '}for {estimatedCost.units} {agent.variable_pricing.unit_label}{estimatedCost.units !== 1 ? 's' : ''}
                </span>
              )}
            </span>
          </div>
          <button
            type="button"
            className={`invoke-panel__private-toggle${privateTask ? ' invoke-panel__private-toggle--on' : ''}`}
            onClick={() => setPrivateTask(p => !p)}
            title={privateTask ? 'Private: output will not be saved to work history' : 'Public: output may be saved as a work example'}
          >
            {privateTask ? <Lock size={11} /> : <Unlock size={11} />}
            {privateTask ? 'Private task' : 'Public task'}
          </button>
          <Button
            type="submit"
            variant="primary"
            size="md"
            loading={loading}
            className="invoke-panel__submit"
            icon={mode === 'async' ? <Radio size={14} /> : <Zap size={14} />}
          >
            {mode === 'async' ? `Create async job · ${price}` : `Run now · ${price}`}
          </Button>
        </div>
      </form>
    )
  }

  const isLastField = step === total - 1
  const f = fields[step]

  return (
    <form className="invoke-panel" onSubmit={handleSubmit}>
      {/* Progress bar */}
      <div className="invoke-panel__progress-track">
        <div
          className="invoke-panel__progress-fill"
          style={{ width: `${progressPct}%` }}
        />
      </div>

      {/* Question pane - AnimatePresence for clean step transitions */}
      <div className="invoke-panel__questions">
        <AnimatePresence mode="wait">
          <motion.div
            key={step}
            className="invoke-panel__q active"
            variants={STEP_VARIANTS}
            initial="initial"
            animate="animate"
            exit="exit"
            transition={STEP_TRANSITION}
          >
            <span className="invoke-panel__q-num">
              {String(step + 1).padStart(2, '0')} of {String(total).padStart(2, '0')}
            </span>
            <label className="invoke-panel__q-label" htmlFor={`tf-${f.name}`}>
              {f.label ?? f.name}
              {f.required && <span style={{ color: 'var(--accent)', marginLeft: 4 }}>*</span>}
            </label>
            {f.hint && <p className="invoke-panel__q-hint">{f.hint}</p>}

            {/* Input */}
            {f.type === 'textarea' ? (
              <textarea
                id={`tf-${f.name}`}
                ref={inputRef}
                className="invoke-panel__tf-textarea"
                placeholder={f.placeholder || 'Type your answer here…'}
                value={values[f.name]}
                onChange={e => set(f.name, e.target.value)}
                required={f.required}
                maxLength={f.max_length}
                onKeyDown={handleKeyDown}
              />
            ) : f.type === 'select' ? (
              <select
                id={`tf-${f.name}`}
                ref={inputRef}
                className="invoke-panel__tf-select"
                value={values[f.name]}
                onChange={e => { set(f.name, e.target.value); setTimeout(goNext, 200) }}
                required={f.required}
              >
                <option value="">Select an option…</option>
                {(f.options ?? []).map(o => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <input
                id={`tf-${f.name}`}
                ref={inputRef}
                className="invoke-panel__tf-input"
                type="text"
                placeholder={f.placeholder || 'Type your answer…'}
                value={values[f.name]}
                onChange={e => set(f.name, e.target.value)}
                required={f.required}
                maxLength={f.max_length}
                onKeyDown={handleKeyDown}
                autoComplete="off"
              />
            )}

            {/* Action row */}
            <div className="invoke-panel__q-row">
              {f.type !== 'select' && !isLastField && (
                <button type="button" className="invoke-panel__ok" onClick={goNext}>
                  Next →
                </button>
              )}
              {step > 0 && (
                <button type="button" className="invoke-panel__back" onClick={goBack}>
                  ← Back
                </button>
              )}
            </div>
          </motion.div>
        </AnimatePresence>
      </div>

      {/* Footer: mode selector + price + submit */}
      <div className="invoke-panel__footer">
        <Segmented options={MODE_OPTIONS} value={mode} onChange={onModeChange} />
        <p className="invoke-panel__mode-help">
          {mode === 'async'
            ? 'Async queues a job you can monitor in Jobs.'
            : 'Sync returns output immediately in this panel.'}
        </p>
        {inputError && <p className="invoke-panel__error-text" role="alert">{inputError}</p>}
        <div className="invoke-panel__price-bar">
          <span className="invoke-panel__price-label">
            {estimatedCost != null
              ? 'Estimated cost'
              : agent?.variable_pricing
                ? 'Price varies by usage'
                : 'Cost per call'}
          </span>
          <span className="invoke-panel__price-val">
            {price}
            {estimatedCost != null && agent?.variable_pricing?.unit_label && (
              <span className="invoke-panel__price-hint">
                {' '}for {estimatedCost.units} {agent.variable_pricing.unit_label}{estimatedCost.units !== 1 ? 's' : ''}
              </span>
            )}
          </span>
        </div>
        <button
          type="button"
          className={`invoke-panel__private-toggle${privateTask ? ' invoke-panel__private-toggle--on' : ''}`}
          onClick={() => setPrivateTask(p => !p)}
          title={privateTask ? 'Private: output will not be saved to work history' : 'Public: output may be saved as a work example'}
        >
          {privateTask ? <Lock size={11} /> : <Unlock size={11} />}
          {privateTask ? 'Private task' : 'Public task'}
        </button>
        <Button
          type="submit"
          variant="primary"
          size="md"
          loading={loading}
          className="invoke-panel__submit"
          icon={mode === 'async' ? <Radio size={14} /> : <Zap size={14} />}
        >
          {mode === 'async' ? `Create async job · ${price}` : `Run now · ${price}`}
        </Button>
      </div>
    </form>
  )
}
