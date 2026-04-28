import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fmtDate, fmtDateSec, fmtUsd, fmtMs, relativeTime } from './format.js'

describe('fmtDate', () => {
  it('returns — for null', () => expect(fmtDate(null)).toBe('—'))
  it('returns — for undefined', () => expect(fmtDate(undefined)).toBe('—'))
  it('returns — for empty string', () => expect(fmtDate('')).toBe('—'))
  it('returns — for invalid date string', () => expect(fmtDate('not-a-date')).toBe('—'))
  it('formats a valid ISO timestamp', () => {
    const result = fmtDate('2024-06-15T14:30:00Z')
    expect(typeof result).toBe('string')
    expect(result).not.toBe('—')
    expect(result.length).toBeGreaterThan(3)
  })
})

describe('fmtDateSec', () => {
  it('returns — for null', () => expect(fmtDateSec(null)).toBe('—'))
  it('returns — for invalid date', () => expect(fmtDateSec('bad')).toBe('—'))
  it('formats a valid ISO timestamp with seconds', () => {
    const result = fmtDateSec('2024-06-15T14:30:45Z')
    expect(typeof result).toBe('string')
    expect(result).not.toBe('—')
  })
})

describe('fmtUsd', () => {
  it('returns -- for null', () => expect(fmtUsd(null)).toBe('--'))
  it('returns -- for undefined', () => expect(fmtUsd(undefined)).toBe('--'))
  it('returns -- for NaN', () => expect(fmtUsd(NaN)).toBe('--'))
  it('returns -- for a string', () => expect(fmtUsd('10')).toBe('--'))
  it('formats zero cents as $0.00', () => expect(fmtUsd(0)).toMatch(/\$0\.00/))
  it('formats 100 cents as $1.00', () => expect(fmtUsd(100)).toMatch(/1\.00/))
  it('formats 1999 cents as $19.99', () => expect(fmtUsd(1999)).toMatch(/19\.99/))
  it('formats negative values (refunds)', () => {
    const result = fmtUsd(-500)
    expect(result).toMatch(/5\.00/)
  })
})

describe('fmtMs', () => {
  it('returns - for null', () => expect(fmtMs(null)).toBe('-'))
  it('returns - for undefined', () => expect(fmtMs(undefined)).toBe('-'))
  it('returns - for NaN', () => expect(fmtMs(NaN)).toBe('-'))
  it('returns - for string', () => expect(fmtMs('100')).toBe('-'))
  it('formats 0 ms', () => expect(fmtMs(0)).toBe('0 ms'))
  it('formats whole ms', () => expect(fmtMs(250)).toBe('250 ms'))
  it('rounds fractional ms', () => expect(fmtMs(250.7)).toBe('251 ms'))
  it('formats large values', () => expect(fmtMs(12000)).toBe('12000 ms'))
})

describe('relativeTime', () => {
  let now

  beforeEach(() => {
    now = Date.now()
    vi.useFakeTimers()
    vi.setSystemTime(now)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns null for null', () => expect(relativeTime(null)).toBeNull())
  it('returns null for undefined', () => expect(relativeTime(undefined)).toBeNull())
  it('returns null for empty string', () => expect(relativeTime('')).toBeNull())

  it('returns "just now" for < 1 minute ago', () => {
    const iso = new Date(now - 30_000).toISOString()
    expect(relativeTime(iso)).toBe('just now')
  })

  it('returns Xm ago for < 1 hour', () => {
    const iso = new Date(now - 5 * 60_000).toISOString()
    expect(relativeTime(iso)).toBe('5m ago')
  })

  it('returns Xh ago for < 1 day', () => {
    const iso = new Date(now - 3 * 3600_000).toISOString()
    expect(relativeTime(iso)).toBe('3h ago')
  })

  it('returns X day ago (singular) for 1 day', () => {
    const iso = new Date(now - 1 * 86400_000).toISOString()
    expect(relativeTime(iso)).toBe('1 day ago')
  })

  it('returns X days ago for 2–6 days', () => {
    const iso = new Date(now - 3 * 86400_000).toISOString()
    expect(relativeTime(iso)).toBe('3 days ago')
  })

  it('returns X week ago (singular) for 1 week', () => {
    const iso = new Date(now - 7 * 86400_000).toISOString()
    expect(relativeTime(iso)).toBe('1 week ago')
  })

  it('returns X weeks ago for 2+ weeks', () => {
    const iso = new Date(now - 14 * 86400_000).toISOString()
    expect(relativeTime(iso)).toBe('2 weeks ago')
  })
})
