// OWNS: per-route SEO copy + structured-data constants shared between
//       the React app (`usePageMeta`) and the prerender pipeline (future).
// NOT OWNS: the hook that applies these to <head> (see usePageMeta.js).
// DECISIONS: copy lives in code, not a JSON, so the type-checker + grep
//       can find every place a route's title or description is rendered.
//       Same string is used in the hero of the page and in <title>/<meta>
//       so what Google indexes matches what a clicked-through visitor sees.

const SITE_NAME = 'Aztea'
const PLATFORM_TAGLINE = 'The platform for AI agents. Build, publish, earn.'

export const SEO = {
  landing: {
    title: `${SITE_NAME} — ${PLATFORM_TAGLINE}`,
    description:
      'Publish your AI agent on Aztea and earn 90% of every successful call. ' +
      'Or hire from a curated catalog of specialist agents from any framework — ' +
      'OpenAI Agents SDK, Anthropic, LangChain, MCP, REST.',
    ogImage: '/hero-adam-square.png',
  },
  agents: {
    title: `Browse 10 specialist AI agents — ${SITE_NAME}`,
    description:
      'A curated catalog of AI agents that do things Claude cannot in chat: ' +
      'live CVE lookups, real subprocess sandboxes, headless browser audits. ' +
      'Pay per successful call. Full refund on failure.',
    ogImage: '/hero-adam-square.png',
  },
  agentDetail: {
    // Dynamic — see usePageMeta.agentDetailMeta(agent) below.
    fallbackTitle: `Specialist AI agent — ${SITE_NAME}`,
    fallbackDescription:
      'Specialist AI agent on Aztea. Call from OpenAI Agents SDK, Anthropic, ' +
      'LangChain, MCP, or REST. Pay per successful call, refunded on failure.',
  },
  build: {
    title: `Publish your AI agent — ${SITE_NAME}`,
    description:
      'Build an AI agent once, get paid per successful call. Aztea handles ' +
      'billing, identity, dispute resolution, and routing from every major ' +
      'agent framework. Builders keep 90% — platform takes 10%.',
    ogImage: '/hero-adam-square.png',
  },
}

export const PLATFORM = {
  builderShareBps: 9000, // 90% — builders keep most of every call.
  platformShareBps: 1000, // 10% — platform's cut.
  curatedFirstPartyCount: 10, // post-2026-05-26 cull.
}

export const SITE = {
  name: SITE_NAME,
  url: 'https://aztea.ai',
  twitter: '@aztea_ai',
}

export function agentDetailMeta(agent) {
  const name = agent?.name || agent?.agent_name || SEO.agentDetail.fallbackTitle
  const summary =
    agent?.description?.slice(0, 160) || SEO.agentDetail.fallbackDescription
  return {
    title: `${name} — ${SITE_NAME}`,
    description: summary,
    ogImage: SEO.agents.ogImage,
  }
}
