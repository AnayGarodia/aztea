import './Button.css'
import { Loader2 } from 'lucide-react'

export default function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled = false,
  icon,
  iconRight,
  children,
  className = '',
  ...props
}) {
  return (
    <button
      className={`btn btn--${variant} btn--${size} ${className}`}
      disabled={disabled || loading}
      {...props}
    >
      {loading
        ? <Loader2 size={14} className="btn__spinner" />
        : icon}
      {children}
      {!loading && iconRight}
    </button>
  )
}
