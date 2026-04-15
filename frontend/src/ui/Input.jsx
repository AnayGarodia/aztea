import { useId } from 'react'
import './Input.css'

export default function Input({
  label,
  hint,
  iconLeft,
  iconRight,
  mono = false,
  className = '',
  wrapClassName = '',
  ...props
}) {
  const generatedId = useId()
  const inputId = props.id ?? generatedId
  const hintId = hint ? `${inputId}-hint` : undefined

  return (
    <div className={`input-wrap ${wrapClassName}`}>
      {label && <label className="input-label" htmlFor={inputId}>{label}</label>}
      <div className="input-field-wrap">
        {iconLeft && <span className="input-icon-left">{iconLeft}</span>}
        <input
          id={inputId}
          aria-describedby={hintId}
          className={`input ${iconLeft ? 'input--has-left' : ''} ${iconRight ? 'input--has-right' : ''} ${mono ? 'input--mono' : ''} ${className}`}
          {...props}
        />
        {iconRight && <span className="input-icon-right">{iconRight}</span>}
      </div>
      {hint && <p className="input-hint" id={hintId}>{hint}</p>}
    </div>
  )
}
