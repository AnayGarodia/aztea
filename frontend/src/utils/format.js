/**
 * Shared display-formatting utilities.
 * All functions are pure and safe to call with null/undefined.
 */

/**
 * Format an ISO timestamp as "Mon D, HH:MM" (locale-aware).
 * Returns '—' for null/empty input.
 */
export function fmtDate(str) {
  if (!str) return '—'
  const d = new Date(str)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/**
 * Format an ISO timestamp with seconds — useful for high-frequency events.
 * Returns '—' for null/empty input.
 */
export function fmtDateSec(str) {
  if (!str) return '—'
  const d = new Date(str)
  if (isNaN(d.getTime())) return '—'
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/**
 * Format a cents integer as a USD string ("$1.23").
 * Returns '--' for non-numeric input.
 */
export function fmtUsd(cents) {
  if (typeof cents !== 'number' || isNaN(cents)) return '--'
  return (cents / 100).toLocaleString(undefined, { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/**
 * Format a milliseconds number as "N ms".
 * Returns '-' for non-numeric input.
 */
export function fmtMs(value) {
  if (typeof value !== 'number' || isNaN(value)) return '-'
  return `${Math.round(value)} ms`
}

/**
 * Format an ISO timestamp as date only: "Mon D, YYYY" (no time).
 * Returns the sentinel string for null/empty input.
 */
export function fmtDateShort(str, sentinel = '—') {
  if (!str) return sentinel
  const d = new Date(str)
  if (isNaN(d.getTime())) return sentinel
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

/**
 * Return a human-readable relative time string ("just now", "5m ago", "2h ago", etc.).
 * Returns null for falsy input.
 */
export function relativeTime(isoString) {
  if (!isoString) return null
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  if (days < 7) return `${days} day${days !== 1 ? 's' : ''} ago`
  const weeks = Math.floor(days / 7)
  return `${weeks} week${weeks !== 1 ? 's' : ''} ago`
}
