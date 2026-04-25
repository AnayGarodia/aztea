import { useState, useMemo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Copy } from 'lucide-react'
import './MarkdownDoc.css'

// Matches placeholders like <YOUR_API_KEY>, <YOUR_AGENT_ID>, <JOB_ID>, etc.
// Convention: angle-bracketed UPPER_SNAKE tokens are user-filled values.
const PLACEHOLDER_RE = /<[A-Z][A-Z0-9_]{2,}>/g

function extractText(children) {
  if (children == null) return ''
  if (typeof children === 'string' || typeof children === 'number') return String(children)
  if (Array.isArray(children)) return children.map(extractText).join('')
  if (typeof children === 'object' && children.props) return extractText(children.props.children)
  return ''
}

function highlightPlaceholders(text) {
  if (!text) return text
  const out = []
  let lastIdx = 0
  let match
  PLACEHOLDER_RE.lastIndex = 0
  while ((match = PLACEHOLDER_RE.exec(text)) !== null) {
    if (match.index > lastIdx) out.push(text.slice(lastIdx, match.index))
    out.push(
      <span
        key={`ph-${match.index}`}
        className="doc-placeholder"
        title="Replace with your own value"
      >
        {match[0]}
      </span>
    )
    lastIdx = match.index + match[0].length
  }
  if (lastIdx < text.length) out.push(text.slice(lastIdx))
  return out.length === 1 && typeof out[0] === 'string' ? out[0] : out
}

// react-markdown v10 removed the `inline` prop from the code component.
// Block code content always ends with a trailing newline; inline code never does.
// We use that to reliably distinguish them.
function CodeBlock({ className, children }) {
  const [copied, setCopied] = useState(false)
  const rawFull = useMemo(() => extractText(children), [children])
  const raw = rawFull.replace(/\n$/, '')
  const isBlock = rawFull.includes('\n')

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(raw)
      setCopied(true)
      setTimeout(() => setCopied(false), 1600)
    } catch {
      // clipboard blocked in some contexts
    }
  }

  if (!isBlock) {
    return <code className={className}>{highlightPlaceholders(raw)}</code>
  }

  return (
    <div className="md-codeblock">
      <button
        type="button"
        className={`md-codeblock__copy ${copied ? 'is-copied' : ''}`}
        onClick={onCopy}
        aria-label="Copy code"
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
        <span>{copied ? 'Copied' : 'Copy'}</span>
      </button>
      <pre className={className}>
        <code className={className}>{highlightPlaceholders(raw)}</code>
      </pre>
    </div>
  )
}

function TextWithPlaceholders({ children }) {
  if (typeof children === 'string') return <>{highlightPlaceholders(children)}</>
  if (Array.isArray(children)) {
    return children.map((c, i) =>
      typeof c === 'string' ? <span key={i}>{highlightPlaceholders(c)}</span> : c
    )
  }
  return children
}

export default function MarkdownDoc({ content, className = '' }) {
  const text = String(content ?? '')
  return (
    <div className={`markdown-doc ${className}`.trim()}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code: CodeBlock,
          // keep <pre> as-is; our CodeBlock renders its own <pre>
          pre: ({ children }) => <>{children}</>,
          p: ({ children }) => <p><TextWithPlaceholders>{children}</TextWithPlaceholders></p>,
          li: ({ children }) => <li><TextWithPlaceholders>{children}</TextWithPlaceholders></li>,
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
