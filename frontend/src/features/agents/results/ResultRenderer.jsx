import FinancialResult  from './FinancialResult'
import CodeReviewResult from './CodeReviewResult'
import TextIntelResult  from './TextIntelResult'
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
  if (hasTag(agent, 'nlp', 'sentiment-analysis', 'text-analytics')) return <TextIntelResult result={result} />
  if (hasTag(agent, 'wikipedia', 'research', 'knowledge-base'))     return <WikiResult result={result} />
  return <GenericResult result={result} />
}
