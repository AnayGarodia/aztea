import { AlertTriangle, X } from 'lucide-react'
import Button from './Button'
import './ErrorBanner.css'

export default function ErrorBanner({
  message,
  title = 'Something went wrong',
  onRetry,
  onDismiss,
  className = '',
}) {
  if (!message) return null
  return (
    <div className={`error-banner ${className}`.trim()} role="alert">
      <div className="error-banner__icon">
        <AlertTriangle size={16} />
      </div>
      <div className="error-banner__content">
        <p className="error-banner__title">{title}</p>
        <p className="error-banner__message">{message}</p>
      </div>
      <div className="error-banner__actions">
        {onRetry && (
          <Button type="button" size="sm" variant="secondary" onClick={onRetry}>
            Retry
          </Button>
        )}
        {onDismiss && (
          <button type="button" className="error-banner__dismiss" onClick={onDismiss} aria-label="Dismiss error">
            <X size={14} />
          </button>
        )}
      </div>
    </div>
  )
}
