import './Segmented.css'

export default function Segmented({ options, value, onChange, className = '' }) {
  return (
    <div className={`segmented ${className}`} role="group">
      {options.map(opt => (
        <button
          key={opt.value}
          className={`segmented__btn ${value === opt.value ? 'segmented__btn--active' : ''}`}
          onClick={() => onChange(opt.value)}
          type="button"
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}
