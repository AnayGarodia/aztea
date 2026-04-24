import FinancialResult  from './FinancialResult'
import CodeReviewResult from './CodeReviewResult'
import WikiResult       from './WikiResult'
import GenericResult    from './GenericResult'

function hasTag(agent, ...tags) {
  const agentTags = new Set(agent?.tags ?? [])
  return tags.some(t => agentTags.has(t))
}

export default function ResultRenderer({ result, agent }) {
  if (!result) return null
  if (hasTag(agent, 'financial-research', 'sec-filings')) return <FinancialResult result={result} />
  if (hasTag(agent, 'code-review', 'security'))           return <CodeReviewResult result={result} />
  if (hasTag(agent, 'wikipedia'))                         return <WikiResult result={result} />
  return <GenericResult result={result} />
}
