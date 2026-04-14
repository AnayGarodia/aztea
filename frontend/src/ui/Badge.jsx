import './Badge.css'

const VARIANT_MAP = {
  // job statuses
  pending:               'default',
  running:               'accent',
  awaiting_clarification:'warn',
  complete:              'positive',
  failed:                'negative',
  // signal
  positive:              'positive',
  negative:              'negative',
  neutral:               'default',
  mixed:                 'warn',
  // severity
  critical:              'negative',
  high:                  'negative',
  medium:                'warn',
  low:                   'default',
  info:                  'info',
  // tx types
  deposit:               'positive',
  refund:                'positive',
  charge:                'negative',
  payout:                'accent',
  fee:                   'default',
}

const PULSE_STATUSES = new Set(['running', 'pending'])

export default function Badge({ variant, label, children, dot = false, className = '', ...props }) {
  const v = variant ?? VARIANT_MAP[label] ?? VARIANT_MAP[children] ?? 'default'
  const pulse = PULSE_STATUSES.has(label ?? children ?? '')
  return (
    <span
      className={`badge badge--${v} ${pulse ? 'badge--pulse' : ''} ${className}`}
      {...props}
    >
      {dot && <span className="badge__dot" style={{ background: 'currentColor' }} />}
      {label ?? children}
    </span>
  )
}
