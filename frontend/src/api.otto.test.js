import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fetchOttoMetrics } from './api.js'

const ORIGINAL_FETCH = globalThis.fetch

function mockJson(payload) {
  globalThis.fetch.mockResolvedValue({
    ok: true,
    status: 200,
    headers: { get: (n) => (n.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  })
}

describe('fetchOttoMetrics', () => {
  beforeEach(() => { globalThis.fetch = vi.fn() })
  afterEach(() => { globalThis.fetch = ORIGINAL_FETCH })

  it('GETs all sections with the window and bearer header when no section is given', async () => {
    const payload = { window: '30d', sections: { overview: { tasks: 3 } } }
    mockJson(payload)

    const body = await fetchOttoMetrics('test-key', { window: '30d' })

    expect(globalThis.fetch).toHaveBeenCalledOnce()
    const [url, init] = globalThis.fetch.mock.calls[0]
    expect(String(url)).toMatch(/\/admin\/otto\/metrics\?/)
    expect(String(url)).toMatch(/window=30d/)
    expect(String(url)).not.toMatch(/section=/)
    expect(init.headers.Authorization).toBe('Bearer test-key')
    expect(body).toEqual(payload)
  })

  it('includes the section param when one is requested', async () => {
    mockJson({ section: 'latency', window: '7d', data: {} })

    await fetchOttoMetrics('k', { section: 'latency', window: '7d' })

    const [url] = globalThis.fetch.mock.calls[0]
    expect(String(url)).toMatch(/section=latency/)
    expect(String(url)).toMatch(/window=7d/)
  })

  it('defaults the window to 30d', async () => {
    mockJson({ sections: {} })
    await fetchOttoMetrics('k')
    const [url] = globalThis.fetch.mock.calls[0]
    expect(String(url)).toMatch(/window=30d/)
  })
})
