import { useState, useMemo, useEffect } from 'react'
import Input from '../../ui/Input'
import Textarea from '../../ui/Textarea'
import Select from '../../ui/Select'
import Button from '../../ui/Button'
import Segmented from '../../ui/Segmented'
import { Zap, Radio } from 'lucide-react'
import './AgentInputForm.css'

const MODE_OPTIONS = [
  { value: 'sync',  label: 'Sync' },
  { value: 'async', label: 'Async' },
]

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

export default function AgentInputForm({ agent, onSubmit, loading, mode, onModeChange }) {
  const fields = useMemo(() => deriveFields(agent?.input_schema), [agent])
  const requiredCount = useMemo(() => fields.filter(f => f.required).length, [fields])
  const [values, setValues] = useState(() =>
    Object.fromEntries(fields.map(f => [f.name, f.default ?? '']))
  )

  // Reset form when agent changes
  useEffect(() => {
    setValues(Object.fromEntries(fields.map(f => [f.name, f.default ?? ''])))
  }, [fields])

  const set = (name, val) => setValues(v => ({ ...v, [name]: val }))

  const handleSubmit = (e) => {
    e.preventDefault()
    const payload = {}
    fields.forEach(f => {
      let v = values[f.name] ?? ''
      if (f.transform === 'uppercase') v = String(v).toUpperCase()
      payload[f.name] = v
    })
    onSubmit(payload)
  }

  const price = `$${Number(agent?.price_per_call_usd ?? 0).toFixed(2)}`

  return (
    <form className="invoke-panel" onSubmit={handleSubmit}>
      <p className="invoke-panel__intro">
        {requiredCount > 0
          ? `${requiredCount} required field${requiredCount > 1 ? 's' : ''} · review schema hints below before sending.`
          : 'No required fields in schema. You can run with defaults or custom payload.'}
      </p>

      {fields.length === 0 && (
        <p className="invoke-panel__no-schema">
          This agent has no defined input schema. Check its documentation.
        </p>
      )}

      {fields.map(f => {
        if (f.type === 'textarea') {
          return (
            <Textarea
              key={f.name}
              label={f.label ?? f.name}
              hint={f.hint}
              placeholder={f.placeholder}
              value={values[f.name]}
              onChange={e => set(f.name, e.target.value)}
              required={f.required}
              maxLength={f.max_length}
              style={{ minHeight: 120 }}
            />
          )
        }
        if (f.type === 'select') {
          return (
            <Select
              key={f.name}
              label={f.label ?? f.name}
              hint={f.hint}
              value={values[f.name]}
              onChange={e => set(f.name, e.target.value)}
              required={f.required}
            >
              <option value="">Select…</option>
              {(f.options ?? []).map(o => <option key={o} value={o}>{o}</option>)}
            </Select>
          )
        }
        return (
          <Input
            key={f.name}
            label={f.label ?? f.name}
            hint={f.hint}
            placeholder={f.placeholder}
            value={values[f.name]}
            onChange={e => set(f.name, e.target.value)}
            required={f.required}
            maxLength={f.max_length}
          />
        )
      })}

      <Segmented options={MODE_OPTIONS} value={mode} onChange={onModeChange} />
      <p className="invoke-panel__mode-help">
        {mode === 'async'
          ? 'Async queues a job you can monitor in Jobs.'
          : 'Sync returns output immediately in this panel.'}
      </p>

      <div className="invoke-panel__price-bar">
        <span className="invoke-panel__price-label">Cost per call</span>
        <span className="invoke-panel__price-val">{price}</span>
      </div>

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
    </form>
  )
}
