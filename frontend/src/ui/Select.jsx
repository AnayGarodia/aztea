import { useId } from 'react'
import './Select.css'
import './Input.css'

export default function Select({ label, hint, className = '', wrapClassName = '', children, ...props }) {
  const generatedId = useId()
  const inputId = props.id ?? generatedId
  const hintId = hint ? `${inputId}-hint` : undefined

  return (
    <div className={`input-wrap ${wrapClassName}`}>
      {label && <label className="input-label" htmlFor={inputId}>{label}</label>}
      <select id={inputId} aria-describedby={hintId} className={`select ${className}`} {...props}>
        {children}
      </select>
      {hint && <p className="input-hint" id={hintId}>{hint}</p>}
    </div>
  )
}
