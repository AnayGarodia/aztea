import { describe, it, expect } from 'vitest'
import {
  normalizeTags,
  validatePublicHttpsUrl,
  validateAgentRegistrationForm,
  validateInvokePayload,
  guardLimits,
} from './inputGuards.js'

// ─── normalizeTags ─────────────────────────────────────────────────────────────

describe('normalizeTags', () => {
  it('splits comma-separated tags', () => {
    expect(normalizeTags('ai, search, nlp')).toEqual(['ai', 'search', 'nlp'])
  })
  it('lowercases tags', () => {
    expect(normalizeTags('AI,Search')).toEqual(['ai', 'search'])
  })
  it('trims whitespace', () => {
    expect(normalizeTags('  ai  ,  search  ')).toEqual(['ai', 'search'])
  })
  it('filters empty entries', () => {
    expect(normalizeTags('ai,,nlp,')).toEqual(['ai', 'nlp'])
  })
  it('handles empty string', () => {
    expect(normalizeTags('')).toEqual([])
  })
  it('handles null/undefined gracefully', () => {
    expect(normalizeTags(null)).toEqual([])
    expect(normalizeTags(undefined)).toEqual([])
  })
})

// ─── validatePublicHttpsUrl ────────────────────────────────────────────────────

describe('validatePublicHttpsUrl', () => {
  it('returns null for a valid HTTPS URL', () => {
    expect(validatePublicHttpsUrl('https://api.example.com/run')).toBeNull()
  })
  it('returns error for empty string', () => {
    expect(validatePublicHttpsUrl('')).toMatch(/required/)
  })
  it('returns error for non-URL input', () => {
    expect(validatePublicHttpsUrl('not a url')).toMatch(/valid URL/)
  })
  it('returns error for http:// (not https)', () => {
    expect(validatePublicHttpsUrl('http://api.example.com/run')).toMatch(/https/)
  })
  it('returns error for localhost', () => {
    expect(validatePublicHttpsUrl('https://localhost:8000/run')).toMatch(/localhost/)
  })
  it('returns error for 127.0.0.1', () => {
    expect(validatePublicHttpsUrl('https://127.0.0.1/run')).toMatch(/localhost/)
  })
  it('returns error for URLs with # fragments', () => {
    expect(validatePublicHttpsUrl('https://api.example.com/run#section')).toMatch(/fragment/)
  })
  it('uses the fieldName in the error message', () => {
    const err = validatePublicHttpsUrl('', 'Endpoint URL')
    expect(err).toMatch(/Endpoint URL/)
  })
})

// ─── validateAgentRegistrationForm ────────────────────────────────────────────

const validForm = {
  name: 'My Test Agent',
  description: 'Searches the web and returns structured results for any query.',
  endpoint_url: 'https://api.example.com/run',
  price_per_call_usd: '0.05',
  tags: 'search, ai',
}

describe('validateAgentRegistrationForm', () => {
  it('returns null for a fully valid form', () => {
    expect(validateAgentRegistrationForm(validForm)).toBeNull()
  })

  it('requires a name', () => {
    expect(validateAgentRegistrationForm({ ...validForm, name: '' })).toMatch(/name/)
  })
  it('requires name >= 3 chars', () => {
    expect(validateAgentRegistrationForm({ ...validForm, name: 'AB' })).toMatch(/3 characters/)
  })
  it('rejects name > 100 chars', () => {
    expect(validateAgentRegistrationForm({ ...validForm, name: 'A'.repeat(101) })).toMatch(/100/)
  })

  it('requires a description', () => {
    expect(validateAgentRegistrationForm({ ...validForm, description: '' })).toMatch(/Description/)
  })
  it('requires description >= 10 chars', () => {
    expect(validateAgentRegistrationForm({ ...validForm, description: 'Short.' })).toMatch(/10/)
  })
  it('requires description >= 3 words', () => {
    expect(validateAgentRegistrationForm({ ...validForm, description: 'One-word-description.' })).toMatch(/3 words/)
  })
  it('rejects description > 2000 chars', () => {
    expect(validateAgentRegistrationForm({ ...validForm, description: 'word '.repeat(401) })).toMatch(/2000/)
  })

  it('rejects an invalid endpoint URL', () => {
    expect(validateAgentRegistrationForm({ ...validForm, endpoint_url: 'not-a-url' })).toMatch(/valid URL/)
  })
  it('rejects HTTP endpoint', () => {
    expect(validateAgentRegistrationForm({ ...validForm, endpoint_url: 'http://example.com/run' })).toMatch(/https/)
  })

  it('rejects negative price', () => {
    expect(validateAgentRegistrationForm({ ...validForm, price_per_call_usd: '-1' })).toMatch(/non-negative/)
  })
  it('rejects price > $25', () => {
    expect(validateAgentRegistrationForm({ ...validForm, price_per_call_usd: '26' })).toMatch(/Maximum/)
  })
  it('accepts price of 0 (free agents)', () => {
    expect(validateAgentRegistrationForm({ ...validForm, price_per_call_usd: '0' })).toBeNull()
  })

  it('rejects too many tags', () => {
    const tags = Array.from({ length: 11 }, (_, i) => `tag${i}`).join(',')
    expect(validateAgentRegistrationForm({ ...validForm, tags })).toMatch(/10 tags/)
  })
  it('rejects a tag that is too long', () => {
    const tags = 'a'.repeat(33)
    expect(validateAgentRegistrationForm({ ...validForm, tags })).toMatch(/too long/)
  })
})

// ─── validateInvokePayload ─────────────────────────────────────────────────────

describe('validateInvokePayload', () => {
  it('returns null for a simple valid payload', () => {
    expect(validateInvokePayload({ query: 'hello' })).toBeNull()
  })
  it('returns null for an empty object', () => {
    expect(validateInvokePayload({})).toBeNull()
  })

  it('rejects an array as the top-level payload', () => {
    expect(validateInvokePayload(['a', 'b'])).toMatch(/JSON object/)
  })
  it('rejects a string', () => {
    expect(validateInvokePayload('hello')).toMatch(/JSON object/)
  })
  it('rejects null', () => {
    expect(validateInvokePayload(null)).toMatch(/JSON object/)
  })

  it('rejects a string field that exceeds max length', () => {
    const payload = { text: 'x'.repeat(4001) }
    expect(validateInvokePayload(payload)).toMatch(/too long/)
  })

  it('rejects an array with too many items', () => {
    const payload = { items: Array(201).fill(1) }
    expect(validateInvokePayload(payload)).toMatch(/too many items/)
  })

  it('rejects a payload that is too deeply nested', () => {
    let obj = {}
    let cursor = obj
    for (let i = 0; i < 10; i++) {
      cursor.child = {}
      cursor = cursor.child
    }
    expect(validateInvokePayload(obj)).toMatch(/too deeply nested/)
  })

  it('rejects a payload exceeding the byte limit', () => {
    // Use many small strings so string-length and key-count limits aren't hit first
    const payload = {}
    for (let i = 0; i < 100; i++) {
      payload[`field${i}`] = 'x'.repeat(700) // 100 * 700 = 70 000 bytes > 64 KB
    }
    expect(validateInvokePayload(payload)).toMatch(/too large/)
  })

  it('accepts a nested payload within limits', () => {
    const payload = { a: { b: { c: { d: 'value' } } } }
    expect(validateInvokePayload(payload)).toBeNull()
  })
})

// ─── guardLimits ──────────────────────────────────────────────────────────────

describe('guardLimits', () => {
  it('exports MAX_AGENT_PRICE_USD as 25', () => {
    expect(guardLimits.MAX_AGENT_PRICE_USD).toBe(25)
  })
  it('exports MAX_TAGS as 10', () => {
    expect(guardLimits.MAX_TAGS).toBe(10)
  })
})
