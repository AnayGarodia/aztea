import { useState } from 'react'
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

export default function AgentInputForm({ agent, onSubmit, loading, mode, onModeChange }) {
  const fields = agent?.input_schema?.fields ?? []
  const [values, setValues] = useState(() =>
    Object.fromEntries(fields.map(f => [f.name, f.default ?? '']))
  )

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
        {mode === 'async' ? `Queue job · ${price}` : `Invoke · ${price}`}
      </Button>
    </form>
  )
}
