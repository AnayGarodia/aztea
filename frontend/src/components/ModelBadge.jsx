import './ModelBadge.css'

const PROVIDER_META = {
  groq:        { bg: '#fff4e6', fg: '#ad3f00', label: 'Groq' },
  openai:      { bg: '#e6fbf4', fg: '#0a6b4b', label: 'OpenAI' },
  anthropic:   { bg: '#f3ecff', fg: '#4a2a9c', label: 'Anthropic' },
  cohere:      { bg: '#e6f4ff', fg: '#0050a0', label: 'Cohere' },
  bedrock:     { bg: '#fff3e0', fg: '#b35900', label: 'Bedrock' },
  grok:        { bg: '#f0f0f0', fg: '#111111', label: 'Grok' },
  kimi:        { bg: '#e8f5e9', fg: '#1b5e20', label: 'Kimi' },
  gemini:      { bg: '#e8f0fe', fg: '#1a56cc', label: 'Gemini' },
  mistral:     { bg: '#fff8e1', fg: '#8a5500', label: 'Mistral' },
  together:    { bg: '#f3e5f5', fg: '#6a1b9a', label: 'Together' },
  fireworks:   { bg: '#fce4ec', fg: '#880e4f', label: 'Fireworks' },
  deepseek:    { bg: '#e0f7fa', fg: '#00607a', label: 'DeepSeek' },
  perplexity:  { bg: '#e8eaf6', fg: '#283593', label: 'Perplexity' },
  cerebras:    { bg: '#fff9c4', fg: '#7b6000', label: 'Cerebras' },
  openrouter:  { bg: '#fafafa', fg: '#333333', label: 'OpenRouter' },
  sambanova:   { bg: '#ffe0b2', fg: '#bf360c', label: 'SambaNova' },
  novita:      { bg: '#e8f5e9', fg: '#256029', label: 'Novita' },
  ai21:        { bg: '#e3f2fd', fg: '#0d47a1', label: 'AI21' },
  deepinfra:   { bg: '#efebe9', fg: '#4e342e', label: 'DeepInfra' },
  hyperbolic:  { bg: '#fce4ec', fg: '#c62828', label: 'Hyperbolic' },
  anyscale:    { bg: '#e0f2f1', fg: '#00695c', label: 'Anyscale' },
  octoai:      { bg: '#f1f8e9', fg: '#33691e', label: 'OctoAI' },
  nvidia:      { bg: '#e8f5e9', fg: '#1b5e20', label: 'NVIDIA' },
  huggingface: { bg: '#fffde7', fg: '#856100', label: 'HuggingFace' },
  lepton:      { bg: '#e8eaf6', fg: '#1a237e', label: 'Lepton' },
  predibase:   { bg: '#fce4ec', fg: '#880e4f', label: 'Predibase' },
  azure:       { bg: '#e3f2fd', fg: '#0d47a1', label: 'Azure' },
  ollama:      { bg: '#f3e5f5', fg: '#4a148c', label: 'Ollama' },
  lmstudio:    { bg: '#f1f8e9', fg: '#1b5e20', label: 'LM Studio' },
  openai_compat: { bg: '#f1f5f9', fg: '#334155', label: 'OpenAI-compat' },
}

const FALLBACK = { bg: '#f1f5f9', fg: '#334155' }

export default function ModelBadge({ provider, modelId, size = 'sm' }) {
  if (!provider) return null
  const key = provider.toLowerCase().replace(/[-_\s]/g, '')
  const meta = PROVIDER_META[provider] ?? PROVIDER_META[key] ?? FALLBACK
  const label = meta.label ?? provider
  return (
    <span
      className={`model-badge model-badge--${size}`}
      style={{ background: meta.bg, color: meta.fg }}
      title={modelId ? `${label} · ${modelId}` : label}
    >
      {label}{modelId ? <span className="model-badge__model"> · {modelId}</span> : null}
    </span>
  )
}
