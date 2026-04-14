import './Select.css'
import './Input.css'

export default function Select({ label, hint, className = '', wrapClassName = '', children, ...props }) {
  return (
    <div className={`input-wrap ${wrapClassName}`}>
      {label && <label className="input-label">{label}</label>}
      <select className={`select ${className}`} {...props}>
        {children}
      </select>
      {hint && <p className="input-hint">{hint}</p>}
    </div>
  )
}
