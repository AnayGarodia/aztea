import './OrnamentalDivider.css'

// Tiny copper line + diamond + line ornament. Sits under section labels or
// between section breaks. Architectural, very subtle.
export default function OrnamentalDivider({ className = '', align = 'left' }) {
  return (
    <div className={`orn-div orn-div--${align} ${className}`.trim()} aria-hidden="true">
      <span className="orn-div__line" />
      <span className="orn-div__diamond" />
      <span className="orn-div__dot" />
      <span className="orn-div__diamond" />
      <span className="orn-div__line" />
    </div>
  )
}
