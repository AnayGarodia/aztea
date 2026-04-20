import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import './MarkdownDoc.css'

export default function MarkdownDoc({ content, className = '' }) {
  const text = String(content ?? '')
  return (
    <div className={`markdown-doc ${className}`.trim()}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {text}
      </ReactMarkdown>
    </div>
  )
}
