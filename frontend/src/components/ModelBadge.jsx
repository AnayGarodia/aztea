import './ModelBadge.css'

const COLORS = {
  groq:      { bg: '#fff4e6', fg: '#ad3f00' },
  openai:    { bg: '#e6fbf4', fg: '#0a6b4b' },
  anthropic: { bg: '#f3ecff', fg: '#4a2a9c' },
  other:     { bg: '#f1f5f9', fg: '#334155' },
}

export default function ModelBadge({ provider, modelId }) {
  if (!provider) return null
  const c = COLORS[provider] ?? COLORS.other
  return (
    <span className="model-badge" style={{ background: c.bg, color: c.fg }}>
      {provider}{modelId ? ` · ${modelId}` : ''}
    </span>
  )
}
