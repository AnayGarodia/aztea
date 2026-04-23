const MAX_AGENT_PRICE_USD = 25
const MAX_TAGS = 10
const MAX_TAG_LENGTH = 32
const MAX_INVOKE_PAYLOAD_BYTES = 64 * 1024
const MAX_INVOKE_NESTING_DEPTH = 8
const MAX_INVOKE_OBJECT_KEYS = 120
const MAX_INVOKE_ARRAY_ITEMS = 200
const MAX_INVOKE_STRING_LENGTH = 4000

function isPlainObject(value) {
  return value != null && typeof value === 'object' && !Array.isArray(value)
}

export function normalizeTags(raw) {
  return String(raw ?? '')
    .split(',')
    .map((tag) => tag.trim().toLowerCase())
    .filter(Boolean)
}

export function validatePublicHttpsUrl(raw, fieldName = 'URL') {
  const url = String(raw ?? '').trim()
  if (!url) return `${fieldName} is required.`
  let parsed
  try {
    parsed = new URL(url)
  } catch {
    return `${fieldName} must be a valid URL, like https://your-agent.example.com/run.`
  }
  if (parsed.protocol !== 'https:') {
    return `${fieldName} must start with https:// so callers can reach it securely.`
  }
  const host = parsed.hostname.toLowerCase()
  const localHosts = new Set(['localhost', '127.0.0.1', '::1', '0.0.0.0'])
  if (localHosts.has(host) || host.endsWith('.local')) {
    return `${fieldName} cannot point to localhost/private network addresses. Use a public hostname.`
  }
  if (parsed.hash) {
    return `${fieldName} should not include #fragments. Paste only the endpoint URL.`
  }
  return null
}

export function validateAgentRegistrationForm(form) {
  const name = String(form?.name ?? '').trim()
  if (!name) return 'Agent name is required.'
  if (name.length < 3) return 'Agent name must be at least 3 characters.'
  if (name.length > 100) return 'Agent name must be 100 characters or fewer.'

  const description = String(form?.description ?? '').trim()
  if (!description) return 'Description is required.'
  if (description.length < 10) return 'Description must be at least 10 characters.'
  if (description.length > 2000) return 'Description must be 2000 characters or fewer.'
  if (description.split(/\s+/).length < 3) {
    return 'Description must use at least 3 words so callers understand your agent.'
  }

  const endpointError = validatePublicHttpsUrl(form?.endpoint_url, 'Endpoint URL')
  if (endpointError) return endpointError

  if (String(form?.healthcheck_url ?? '').trim()) {
    const healthError = validatePublicHttpsUrl(form.healthcheck_url, 'Healthcheck URL')
    if (healthError) return healthError
  }

  const price = Number.parseFloat(String(form?.price_per_call_usd ?? ''))
  if (!Number.isFinite(price) || price < 0) {
    return 'Price must be a non-negative number (for example 0.05).'
  }
  if (price > MAX_AGENT_PRICE_USD) {
    return `Price is too high. Maximum allowed is $${MAX_AGENT_PRICE_USD.toFixed(2)} per call.`
  }

  const tags = normalizeTags(form?.tags)
  if (tags.length > MAX_TAGS) return `Use at most ${MAX_TAGS} tags.`
  const tooLongTag = tags.find((tag) => tag.length > MAX_TAG_LENGTH)
  if (tooLongTag) return `Tag "${tooLongTag.slice(0, 20)}..." is too long (max ${MAX_TAG_LENGTH} chars).`

  return null
}

function walkPayload(value, depth, state) {
  if (depth > MAX_INVOKE_NESTING_DEPTH) {
    throw new Error(`Input is too deeply nested. Keep payload depth at ${MAX_INVOKE_NESTING_DEPTH} levels or less.`)
  }
  if (typeof value === 'string' && value.length > MAX_INVOKE_STRING_LENGTH) {
    throw new Error(`A text field is too long. Limit text fields to ${MAX_INVOKE_STRING_LENGTH} characters.`)
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_INVOKE_ARRAY_ITEMS) {
      throw new Error(`One list has too many items. Maximum ${MAX_INVOKE_ARRAY_ITEMS} items per list.`)
    }
    for (const item of value) walkPayload(item, depth + 1, state)
    return
  }
  if (!isPlainObject(value)) return
  const keys = Object.keys(value)
  state.totalKeys += keys.length
  if (state.totalKeys > MAX_INVOKE_OBJECT_KEYS) {
    throw new Error(`Input has too many fields. Keep total field count under ${MAX_INVOKE_OBJECT_KEYS}.`)
  }
  for (const key of keys) {
    const normalized = key.trim()
    if (!normalized) throw new Error('Input contains an empty field name. Rename that field and try again.')
    if (normalized.length > 100) throw new Error('Input contains a field name that is too long (max 100 chars).')
    walkPayload(value[key], depth + 1, state)
  }
}

export function validateInvokePayload(payload) {
  if (!isPlainObject(payload)) {
    return 'Input payload must be a JSON object (key-value pairs), not a list or plain text.'
  }
  try {
    walkPayload(payload, 0, { totalKeys: 0 })
    const encoded = new TextEncoder().encode(JSON.stringify(payload))
    if (encoded.byteLength > MAX_INVOKE_PAYLOAD_BYTES) {
      return `Input payload is too large. Keep it under ${Math.floor(MAX_INVOKE_PAYLOAD_BYTES / 1024)} KB.`
    }
  } catch (error) {
    return error instanceof Error ? error.message : 'Input payload is invalid.'
  }
  return null
}

export const guardLimits = {
  MAX_AGENT_PRICE_USD,
  MAX_TAGS,
  MAX_TAG_LENGTH,
  MAX_INVOKE_PAYLOAD_BYTES,
}
