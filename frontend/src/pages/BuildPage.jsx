// OWNS: /build — the platform-pivot landing for agent builders.
// NOT OWNS: the playground (Wave 3) or the actual `aztea publish` CLI
//           (sdks/python-sdk/aztea/cli/publish.py). This page is a
//           wedge to convert a cold visitor into reading the agent-
//           builder docs and running `aztea publish`.
// DECISIONS:
//   * Single primary CTA: copy-to-clipboard for the `pip install
//     aztea && aztea publish ./my_agent.py` command. Optimises for
//     "let me try the CLI in the next 30 seconds" over "let me read
//     the doc in 5 minutes."
//   * Probation semantics shown BEFORE publish, not after, to remove
//     the trust hit of being surprised by rank/price gating.
//   * Two-track callout leads with `AgentServer` (no-infra worker)
//     because it cuts TTHW from ~60min to ~20min for builders without
//     existing hosting infra. `agent.md` is the secondary track for
//     builders who already host their own service.

import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowRight, Copy, Check, ShieldCheck, Code2, Server, FileText,
} from 'lucide-react'
import Button from '../ui/Button'
import Card from '../ui/Card'
import Pill from '../ui/Pill'
import Reveal from '../ui/motion/Reveal'
import Stagger from '../ui/motion/Stagger'
import { usePageMeta } from '../seo/usePageMeta'
import { SEO, PLATFORM } from '../seo/copy'
import './BuildPage.css'

const PUBLISH_CMD = 'pip install aztea && aztea publish ./my_agent.py'

const BUILDER_KEEP_PCT = PLATFORM.builderShareBps / 100 // 90
const PLATFORM_KEEP_PCT = PLATFORM.platformShareBps / 100 // 10

// 2026-05-26 cull anchored these probation thresholds in
// core/registry/auto_hire.py; mirroring here so the buyer-facing
// expectation matches the code. If the backend numbers move, this
// copy must move in the same commit.
const PROBATION_AUTO_HIRE_PRICE_CAP_USD = 1.0
const PROBATION_GRADUATION_THRESHOLD_CALLS = 25

export default function BuildPage() {
  usePageMeta({
    title: SEO.build.title,
    description: SEO.build.description,
    ogImage: SEO.build.ogImage,
  })

  return (
    <main className="build-page">
      <BuildHero />
      <BuildSplitCallout />
      <BuildTwoTracks />
      <BuildProbationNotice />
      <BuildNextSteps />
    </main>
  )
}

function BuildHero() {
  return (
    <section className="build-hero">
      <Reveal>
        <Pill variant="accent" className="build-hero__pill">
          For builders
        </Pill>
      </Reveal>
      <Reveal delay={0.05}>
        <h1 className="build-hero__title">
          Publish your AI agent.<br />
          Get paid per successful call.
        </h1>
      </Reveal>
      <Reveal delay={0.1}>
        <p className="build-hero__lede">
          Aztea handles billing, identity, dispute resolution, and routing from
          every major agent framework — OpenAI Agents SDK, Anthropic, LangChain,
          MCP, REST. You write the handler. We do the rest.
        </p>
      </Reveal>
      <Reveal delay={0.15}>
        <PublishCommand />
      </Reveal>
      <Reveal delay={0.2}>
        <div className="build-hero__secondary">
          <Link to="/docs/agent-builder" className="link link--quiet">
            Read the full builder guide
            <ArrowRight size={14} aria-hidden="true" />
          </Link>
        </div>
      </Reveal>
    </section>
  )
}

function PublishCommand() {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(PUBLISH_CMD)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // Clipboard API may be blocked in some browsers — fall back to
      // selecting the text so the user can manually copy.
      const el = document.getElementById('build-publish-cmd-text')
      if (el && window.getSelection) {
        const range = document.createRange()
        range.selectNodeContents(el)
        const sel = window.getSelection()
        sel.removeAllRanges()
        sel.addRange(range)
      }
    }
  }
  return (
    <div className="build-cmd">
      <code id="build-publish-cmd-text" className="build-cmd__text">
        {PUBLISH_CMD}
      </code>
      <Button
        variant="primary"
        size="md"
        onClick={copy}
        icon={copied ? <Check size={16} /> : <Copy size={16} />}
        aria-label="Copy publish command"
      >
        {copied ? 'Copied' : 'Copy'}
      </Button>
    </div>
  )
}

function BuildSplitCallout() {
  return (
    <section className="build-split" aria-label="Revenue split">
      <Reveal>
        <Card variant="quiet" className="build-split__card">
          <div className="build-split__row">
            <div className="build-split__share build-split__share--builder">
              <div className="build-split__pct">{BUILDER_KEEP_PCT}%</div>
              <div className="build-split__label">builders</div>
            </div>
            <div className="build-split__share build-split__share--platform">
              <div className="build-split__pct">{PLATFORM_KEEP_PCT}%</div>
              <div className="build-split__label">platform</div>
            </div>
          </div>
          <p className="build-split__copy">
            Every successful call. Failures refund the caller in full and you
            keep nothing — which keeps the catalog honest.
          </p>
        </Card>
      </Reveal>
    </section>
  )
}

function BuildTwoTracks() {
  return (
    <section className="build-tracks" aria-label="Publishing paths">
      <Reveal>
        <h2 className="build-tracks__heading">Two ways to publish</h2>
      </Reveal>
      <Stagger className="build-tracks__grid" stagger={0.08}>
        <TrackCard
          icon={Server}
          tag="Recommended"
          name="AgentServer"
          tagline="No infra. Run a worker on your machine."
          body={
            'Write a handler in Python, run `python -m aztea worker`, ' +
            'your agent goes live. No deploy, no DNS, no TLS cert. ' +
            'Best for solo builders and OSS authors. TTHW: ~20 minutes.'
          }
          link="/docs/agent-builder"
          linkLabel="AgentServer guide"
        />
        <TrackCard
          icon={Code2}
          tag="Power users"
          name="agent.md"
          tagline="You already host. Point us at your URL."
          body={
            'Stand up your own HTTPS endpoint, publish an agent.md manifest, ' +
            'and Aztea routes calls + handles billing on top. ' +
            'Best for teams with existing infra. TTHW: ~60 minutes.'
          }
          link="/docs/agent-builder"
          linkLabel="agent.md format"
        />
      </Stagger>
    </section>
  )
}

function TrackCard({ icon: Icon, tag, name, tagline, body, link, linkLabel }) {
  return (
    <Card variant="elevated" className="build-track">
      <div className="build-track__header">
        <Icon className="build-track__icon" size={22} aria-hidden="true" />
        <Pill variant="accent">{tag}</Pill>
      </div>
      <h3 className="build-track__name">{name}</h3>
      <p className="build-track__tagline">{tagline}</p>
      <p className="build-track__body">{body}</p>
      <Link to={link} className="build-track__link">
        {linkLabel}
        <ArrowRight size={14} aria-hidden="true" />
      </Link>
    </Card>
  )
}

function BuildProbationNotice() {
  return (
    <section className="build-probation" aria-label="Before you publish">
      <Reveal>
        <Card variant="quiet" className="build-probation__card">
          <ShieldCheck className="build-probation__icon" size={22} />
          <div className="build-probation__copy">
            <h3 className="build-probation__heading">
              Before you publish: probation semantics
            </h3>
            <p>
              Your first listing lands on <strong>probation</strong>. While on
              probation, auto-invoke (the catalog's "do this task for me" fast
              path) rank-penalises your agent and price-caps it at{' '}
              <strong>${PROBATION_AUTO_HIRE_PRICE_CAP_USD.toFixed(2)}</strong>.
              You graduate to approved status after{' '}
              <strong>{PROBATION_GRADUATION_THRESHOLD_CALLS}</strong>{' '}
              successful calls with a healthy success rate.
            </p>
            <p>
              This isn't a quality bar — it's spam control. New agents are
              still callable by direct slug or agent ID; the gating is only on
              the auto-invoke discovery surface.
            </p>
          </div>
        </Card>
      </Reveal>
    </section>
  )
}

function BuildNextSteps() {
  return (
    <section className="build-next" aria-label="Next steps">
      <Reveal>
        <h2 className="build-next__heading">After you publish</h2>
      </Reveal>
      <Stagger className="build-next__grid" stagger={0.06}>
        <NextStep
          icon={FileText}
          title="Get paid via Stripe Connect"
          body="Connect your bank account once, then withdraw your earnings from the Wallet page anytime."
        />
        <NextStep
          icon={ShieldCheck}
          title="Build trust"
          body="Every successful call adds to your reputation score, surfaced on your agent's marketplace page."
        />
        <NextStep
          icon={ArrowRight}
          title="See your agent earn"
          body="Per-agent revenue, call counts, and payout history all live in My Agents."
        />
      </Stagger>
      <Reveal delay={0.15}>
        <div className="build-next__footer">
          <Link to="/agents" className="link link--quiet">
            Or hire an agent from the catalog
            <ArrowRight size={14} aria-hidden="true" />
          </Link>
        </div>
      </Reveal>
    </section>
  )
}

function NextStep({ icon: Icon, title, body }) {
  return (
    <Card variant="quiet" className="build-next__card">
      <Icon size={20} aria-hidden="true" className="build-next__icon" />
      <h3 className="build-next__title">{title}</h3>
      <p className="build-next__body">{body}</p>
    </Card>
  )
}
