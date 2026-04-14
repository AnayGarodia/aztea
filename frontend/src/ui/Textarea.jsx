import './Textarea.css'
import './Input.css'

export default function Textarea({
  label, hint, mono = false,
  className = '', wrapClassName = '',
  ...props
}) {
  return (
    <div className={`input-wrap ${wrapClassName}`}>
      {label && <label className="input-label">{label}</label>}
      <textarea
        className={`textarea ${mono ? 'textarea--mono' : ''} ${className}`}
        {...props}
      />
      {hint && <p className="input-hint">{hint}</p>}
    </div>
  )
}
