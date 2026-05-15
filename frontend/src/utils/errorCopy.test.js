import { describe, it, expect, vi } from 'vitest'

// Stub the api.js module so this test stays focused on the helper's branching
// logic and doesn't drag the real fetch surface into the import graph.
vi.mock('../api', () => ({
  API_ERROR_MESSAGE_BY_CODE: {
    'job.create_failed': 'Job could not be created. Your charge was refunded. Retry shortly.',
  },
}))

const { formatApiError } = await import('./errorCopy.js')

function makeErr({ status, code, message, body }) {
  const err = new Error(message ?? '')
  err.status = status
  err.code = code ?? null
  err.body = body ?? null
  return err
}

describe('formatApiError', () => {
  it('prefers a specific server-authored message over status branching', () => {
    const err = makeErr({
      status: 402,
      message: 'You only have $1.20 available; this job needs $3.00.',
    })
    const { title } = formatApiError(err, { action: 'hire agent' })
    expect(title).toBe('You only have $1.20 available; this job needs $3.00.')
  })

  it('uses API_ERROR_MESSAGE_BY_CODE when server message is generic', () => {
    const err = makeErr({
      status: 500,
      code: 'job.create_failed',
      message: 'Internal Server Error.',
    })
    const { title } = formatApiError(err, { action: 'hire agent' })
    expect(title).toMatch(/Job could not be created/)
  })

  it('branches by status for 401', () => {
    const { title } = formatApiError(makeErr({ status: 401 }), { action: 'list keys' })
    expect(title).toBe('Your session expired. Sign in again.')
  })

  it('branches by status for 402', () => {
    const { title } = formatApiError(makeErr({ status: 402 }), { action: 'hire agent' })
    expect(title).toMatch(/wallet balance/i)
  })

  it('weaves action into 403', () => {
    const { title } = formatApiError(makeErr({ status: 403 }), { action: 'file dispute' })
    expect(title).toMatch(/permission to file dispute/i)
  })

  it('surfaces retry_after seconds on 429', () => {
    const err = makeErr({
      status: 429,
      body: { details: { retry_after_seconds: 12 } },
    })
    const { title } = formatApiError(err, { action: 'create job' })
    expect(title).toBe('Too many requests. Retry in 12s.')
  })

  it('falls back cleanly on 429 without retry_after', () => {
    const { title } = formatApiError(makeErr({ status: 429 }), { action: 'create job' })
    expect(title).toMatch(/wait a moment/i)
  })

  it('surfaces request_id as a hint on 5xx', () => {
    const err = makeErr({ status: 500 })
    err.requestId = 'req_abc123'
    const { hint } = formatApiError(err, { action: 'submit rating' })
    expect(hint).toBe('request_id req_abc123')
  })

  it('marks 429 and 5xx as retryable', () => {
    expect(formatApiError(makeErr({ status: 429 })).retryable).toBe(true)
    expect(formatApiError(makeErr({ status: 503 })).retryable).toBe(true)
    expect(formatApiError(makeErr({ status: 403 })).retryable).toBe(false)
  })

  it('handles a null error without throwing', () => {
    const { title, retryable } = formatApiError(null, { action: 'do thing' })
    expect(title).toBe('Could not do thing. Try again.')
    expect(retryable).toBe(true)
  })
})
