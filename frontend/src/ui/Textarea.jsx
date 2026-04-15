import { useId } from 'react'
import './Textarea.css'
import './Input.css'

export default function Textarea({
  label, hint, mono = false,
  className = '', wrapClassName = '',
  ...props
}) {
  const generatedId = useId()
  const inputId = props.id ?? generatedId
  const hintId = hint ? `${inputId}-hint` : undefined

  return (
    <div className={`input-wrap ${wrapClassName}`}>
      {label && <label className="input-label" htmlFor={inputId}>{label}</label>}
      <textarea
        id={inputId}
        aria-describedby={hintId}
        className={`textarea ${mono ? 'textarea--mono' : ''} ${className}`}
        {...props}
      />
      {hint && <p className="input-hint" id={hintId}>{hint}</p>}
    </div>
  )
}
