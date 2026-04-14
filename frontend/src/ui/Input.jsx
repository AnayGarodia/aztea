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
  return (
    <div className={`input-wrap ${wrapClassName}`}>
      {label && <label className="input-label">{label}</label>}
      <div className="input-field-wrap">
        {iconLeft && <span className="input-icon-left">{iconLeft}</span>}
        <input
          className={`input ${iconLeft ? 'input--has-left' : ''} ${iconRight ? 'input--has-right' : ''} ${mono ? 'input--mono' : ''} ${className}`}
          {...props}
        />
        {iconRight && <span className="input-icon-right">{iconRight}</span>}
      </div>
      {hint && <p className="input-hint">{hint}</p>}
    </div>
  )
}
