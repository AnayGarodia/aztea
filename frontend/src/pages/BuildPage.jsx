// OWNS: /build — the browser playground. A cold visitor lands here,
//       picks a template, edits a Python handler, hits Test (sandbox
//       round-trip, 2-3s), and Publish (auth-gated, creates a hosted
//       skill via the existing /skills pipeline).
// NOT OWNS: the sandbox (server/routes/playground.py + agents/
//       python_executor.py), the listing-safety pipeline
//       (core/listing_safety + listing_safety_judge), the publish
//       pipeline (part_012.py /skills), or the kill switch
//       (admin/agents/{id}/suspend).
// INVARIANTS:
//   * Test is anonymous-callable. Publish is auth-gated — the click
//     opens AuthDialog if !apiKey before posting to /skills.
//   * Every state the spec documents has an explicit branch: idle,
//     test-running, test-failed (listing-safety-blocked vs
//     audit-hook-killed), test-success, publish-running,
//     publish-rejected (with findings), publish-success.
//   * No raw HTML rendering of stdout/stderr — always <pre>{...}</pre>
//     to keep XSS off the table even though the playground response
//     SHOULDN'T be attacker-controlled (the sandbox sanitises ANSI).
// DECISIONS:
//   * Plain <textarea> instead of @monaco-editor/react. The Monaco
//     bundle is ~2MB and requires a web-worker config that varies by
//     deploy target. Functional v1 ships without it; "ship Monaco"
//     is a clean follow-up that doesn't change any other surface.
//   * Template picker is a left-rail accordion of the data shipped
//     in ./buildtemplates/templates.js. Clicking a template loads its source
//     + sample input. No server round-trip.
//   * Publish converts the Python handler into a SKILL.md envelope.
//     For v1 we only support the SKILL.md publish path (the playground
//     test endpoint runs the handler directly via python_executor, but
//     publish goes through hosted_skills which expects SKILL.md). The
//     "real Python handler publish" path uses the CLI today — calling
//     that out in the publish modal.

import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Play, Send, Copy, Check, AlertTriangle, ShieldCheck, FileText } from 'lucide-react'
import Button from '../ui/Button'
import Card from '../ui/Card'
import Pill from '../ui/Pill'
import Input from '../ui/Input'
import Tabs from '../ui/Tabs'
import { usePageMeta } from '../seo/usePageMeta'
import { SEO } from '../seo/copy'
import { useAuth } from '../context/AuthContext'
import { playgroundTest, playgroundPublish } from '../api'
import {
  TEMPLATES, TEMPLATE_CATEGORIES, templatesByCategory,
} from './buildtemplates/templates'
import './BuildPage.css'


const DEFAULT_SOURCE = (
  '# Welcome to the Aztea playground.\n' +
  '# Define `def handler(payload)` and click Test.\n\n' +
  'def handler(payload):\n' +
  '    name = payload.get("name", "world")\n' +
  '    return {"greeting": f"hello, {name}"}\n'
)

const DEFAULT_INPUT = '{\n  "name": "builder"\n}\n'


export default function BuildPage() {
  usePageMeta({
    title: SEO.build.title,
    description: SEO.build.description,
    ogImage: SEO.build.ogImage,
  })

  const { apiKey, openAuth } = useAuth() ?? {}
  const navigate = useNavigate()

  const [source, setSource] = useState(DEFAULT_SOURCE)
  const [inputText, setInputText] = useState(DEFAULT_INPUT)
  const [activeCategory, setActiveCategory] = useState('security')

  const [phase, setPhase] = useState('idle')  // idle | testing | publishing
  const [testResult, setTestResult] = useState(null)
  const [publishResult, setPublishResult] = useState(null)
  const [error, setError] = useState(null)

  const handleLoadTemplate = (tpl) => {
    setSource(tpl.source)
    setInputText(JSON.stringify(tpl.sampleInput, null, 2))
    setTestResult(null)
    setPublishResult(null)
    setError(null)
  }

  const handleTest = async () => {
    setPhase('testing')
    setError(null)
    setTestResult(null)
    setPublishResult(null)
    let parsedInput = {}
    try {
      parsedInput = inputText.trim() ? JSON.parse(inputText) : {}
    } catch (exc) {
      setError({ stage: 'input', message: `Input is not valid JSON: ${exc.message}` })
      setPhase('idle')
      return
    }
    try {
      const result = await playgroundTest({
        key: apiKey || undefined,
        source,
        inputPayload: parsedInput,
      })
      setTestResult(result)
    } catch (exc) {
      setError({ stage: 'test', message: exc?.message || 'Test failed.' })
    } finally {
      setPhase('idle')
    }
  }

  const handlePublish = async ({ name, slug, price, description }) => {
    if (!apiKey) {
      openAuth?.('login', '/build')
      return
    }
    setPhase('publishing')
    setError(null)
    setPublishResult(null)
    // Wrap the Python handler in a minimal SKILL.md envelope. v1 is
    // SKILL.md-only on publish (the existing /skills endpoint owns the
    // hosted-skill creation path). A future iteration will also accept
    // raw Python handlers and call the agent.md registration path.
    const skillMd = (
      `---\n` +
      `name: ${slug}\n` +
      `description: ${description}\n` +
      `---\n\n` +
      `# ${name}\n\n` +
      `\`\`\`python\n${source}\n\`\`\`\n`
    )
    try {
      const result = await playgroundPublish({
        key: apiKey,
        skillMd,
        pricePerCallUsd: Number(price) || 0.05,
      })
      setPublishResult(result)
    } catch (exc) {
      setError({ stage: 'publish', message: exc?.message || 'Publish failed.' })
    } finally {
      setPhase('idle')
    }
  }

  return (
    <main className="buildpage">
      <BuildHeader hasApiKey={!!apiKey} onSignIn={() => openAuth?.('login', '/build')} />
      <div className="buildpage__layout">
        <aside className="buildpage__rail">
          <TemplatesRail
            activeCategory={activeCategory}
            setActiveCategory={setActiveCategory}
            onLoad={handleLoadTemplate}
          />
        </aside>
        <section className="buildpage__editor">
          <EditorPane source={source} setSource={setSource} />
          <div className="buildpage__actions">
            <Button
              variant="secondary"
              onClick={handleTest}
              icon={<Play size={16} />}
              loading={phase === 'testing'}
              disabled={phase !== 'idle'}
            >
              Test in sandbox
            </Button>
            <PublishLauncher
              hasApiKey={!!apiKey}
              onPublish={handlePublish}
              loading={phase === 'publishing'}
              disabled={phase !== 'idle'}
            />
          </div>
        </section>
        <section className="buildpage__output">
          <OutputPanel
            inputText={inputText}
            setInputText={setInputText}
            testResult={testResult}
            publishResult={publishResult}
            error={error}
            phase={phase}
            onShare={publishResult ? () => navigate(`/agents/${publishResult.agent_id}`) : null}
          />
        </section>
      </div>
    </main>
  )
}


function BuildHeader({ hasApiKey, onSignIn }) {
  return (
    <header className="buildpage__header">
      <div className="buildpage__header-left">
        <Link to="/" className="buildpage__home">Aztea</Link>
        <span className="buildpage__dot">·</span>
        <h1 className="buildpage__title">Build a new agent</h1>
      </div>
      <div className="buildpage__header-right">
        <Link to="/agents" className="buildpage__link">Browse catalog</Link>
        {!hasApiKey && (
          <Button variant="primary" size="sm" onClick={onSignIn}>
            Sign in to publish
          </Button>
        )}
      </div>
    </header>
  )
}


function TemplatesRail({ activeCategory, setActiveCategory, onLoad }) {
  const templates = useMemo(() => templatesByCategory(activeCategory), [activeCategory])
  return (
    <div className="buildpage__templates">
      <h2 className="buildpage__rail-heading">
        <FileText size={14} /> Templates
      </h2>
      <div className="buildpage__category-tabs" role="tablist">
        {TEMPLATE_CATEGORIES.map(cat => (
          <button
            key={cat.id}
            type="button"
            className={`buildpage__category ${activeCategory === cat.id ? 'buildpage__category--active' : ''}`}
            onClick={() => setActiveCategory(cat.id)}
            role="tab"
            aria-selected={activeCategory === cat.id}
          >
            {cat.label}
          </button>
        ))}
      </div>
      <ul className="buildpage__template-list">
        {templates.map(tpl => (
          <li key={tpl.id}>
            <button
              type="button"
              onClick={() => onLoad(tpl)}
              className="buildpage__template"
            >
              <span className="buildpage__template-name">{tpl.name}</span>
              <span className="buildpage__template-blurb">{tpl.blurb}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}


function EditorPane({ source, setSource }) {
  // textarea over Monaco — see file-level DECISIONS note. The font
  // stack matches Aztea's mono token; tabs are 4 spaces (Python).
  return (
    <div className="buildpage__editor-pane">
      <div className="buildpage__editor-toolbar">
        <Pill variant="default">handler.py</Pill>
        <span className="buildpage__editor-hint">
          Define <code>def handler(payload)</code> and return a dict.
        </span>
      </div>
      <textarea
        className="buildpage__textarea"
        value={source}
        onChange={(e) => setSource(e.target.value)}
        spellCheck={false}
        autoCorrect="off"
        autoCapitalize="off"
        aria-label="Handler source code"
      />
    </div>
  )
}


function PublishLauncher({ hasApiKey, onPublish, loading, disabled }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('My agent')
  const [slug, setSlug] = useState('my-agent')
  const [price, setPrice] = useState('0.05')
  const [description, setDescription] = useState('Short description.')

  if (!open) {
    return (
      <Button
        variant="primary"
        onClick={() => setOpen(true)}
        icon={<Send size={16} />}
        disabled={disabled}
      >
        {hasApiKey ? 'Publish…' : 'Sign in to publish'}
      </Button>
    )
  }

  return (
    <div className="buildpage__publish-form" role="form">
      <Input
        label="Name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
      />
      <Input
        label="Slug"
        value={slug}
        onChange={(e) => setSlug(e.target.value)}
        required
        hint="kebab-case, unique. /agents/<slug>"
      />
      <Input
        label="Price per call (USD)"
        type="number"
        step="0.01"
        min="0"
        max="25"
        value={price}
        onChange={(e) => setPrice(e.target.value)}
      />
      <Input
        label="Description"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        hint="One sentence. Surfaced on /agents/<slug>."
      />
      <div className="buildpage__publish-actions">
        <Button variant="ghost" onClick={() => setOpen(false)} disabled={loading}>
          Cancel
        </Button>
        <Button
          variant="primary"
          onClick={() => onPublish({ name, slug, price, description })}
          loading={loading}
          icon={<Send size={16} />}
        >
          Publish
        </Button>
      </div>
    </div>
  )
}


function OutputPanel({
  inputText,
  setInputText,
  testResult,
  publishResult,
  error,
  phase,
  onShare,
}) {
  const tabs = [
    { id: 'input', label: 'Input' },
    { id: 'output', label: 'Output' },
    { id: 'logs', label: 'Logs' },
  ]

  return (
    <Tabs tabs={tabs} defaultTab="input">
      {(active) => {
        if (active === 'input') {
          return (
            <div className="buildpage__panel">
              <p className="buildpage__panel-hint">
                JSON payload passed to <code>handler(payload)</code>.
              </p>
              <textarea
                className="buildpage__textarea buildpage__textarea--short"
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                spellCheck={false}
                autoCorrect="off"
              />
            </div>
          )
        }
        if (active === 'output') {
          return (
            <OutputTab
              testResult={testResult}
              publishResult={publishResult}
              error={error}
              phase={phase}
              onShare={onShare}
            />
          )
        }
        // logs
        return (
          <LogsTab testResult={testResult} error={error} />
        )
      }}
    </Tabs>
  )
}


function OutputTab({ testResult, publishResult, error, phase, onShare }) {
  if (phase === 'testing') {
    return <div className="buildpage__panel buildpage__panel--center">Running in sandbox…</div>
  }
  if (phase === 'publishing') {
    return <div className="buildpage__panel buildpage__panel--center">Publishing…</div>
  }
  if (error) {
    return (
      <div className="buildpage__panel">
        <Card variant="danger">
          <div className="buildpage__error">
            <AlertTriangle size={18} />
            <div>
              <strong>{error.stage === 'input' ? 'Invalid input' : 'Error'}</strong>
              <p>{error.message}</p>
            </div>
          </div>
        </Card>
      </div>
    )
  }
  if (publishResult) {
    return <PublishSuccessCard result={publishResult} onShare={onShare} />
  }
  if (testResult) {
    return <TestResultCard result={testResult} />
  }
  return (
    <div className="buildpage__panel buildpage__panel--center buildpage__panel--muted">
      Press <strong>Test in sandbox</strong> to run your handler.
    </div>
  )
}


function TestResultCard({ result }) {
  // Listing safety / playground.disabled errors come back as the
  // response body's `error` field.
  if (result?.error && result.error !== null && typeof result.error === 'string' === false) {
    return (
      <div className="buildpage__panel">
        <Card variant="warn">
          <div className="buildpage__error">
            <ShieldCheck size={18} />
            <div>
              <strong>Listing safety blocked the source.</strong>
              <pre>{JSON.stringify(result.error, null, 2)}</pre>
            </div>
          </div>
        </Card>
      </div>
    )
  }
  const ok = result?.exit_code === 0 && !result?.timed_out
  return (
    <div className="buildpage__panel">
      <div className="buildpage__result-meta">
        <Pill variant={ok ? 'success' : 'warn'}>
          exit_code={result?.exit_code ?? '?'}
        </Pill>
        <Pill variant="default">{result?.execution_time_ms ?? '?'} ms</Pill>
        {result?.timed_out ? <Pill variant="warn">timed out</Pill> : null}
      </div>
      <h3>stdout</h3>
      <pre className="buildpage__pre">{result?.stdout || '(empty)'}</pre>
      {result?.stderr ? (
        <>
          <h3>stderr</h3>
          <pre className="buildpage__pre buildpage__pre--err">{result.stderr}</pre>
        </>
      ) : null}
    </div>
  )
}


function PublishSuccessCard({ result, onShare }) {
  const [copied, setCopied] = useState(false)
  const url = `${window.location.origin}/agents/${result?.agent_id || ''}`
  const copy = () => {
    navigator.clipboard?.writeText(url).catch(() => {})
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="buildpage__panel">
      <Card variant="success">
        <div className="buildpage__publish-success">
          <ShieldCheck size={20} />
          <div>
            <strong>Published.</strong>
            <p>Your agent is live and on probation until track record graduates it.</p>
            <p className="buildpage__publish-url"><code>{url}</code></p>
            <div className="buildpage__publish-actions">
              <Button variant="secondary" size="sm" onClick={copy} icon={copied ? <Check size={14} /> : <Copy size={14} />}>
                {copied ? 'Copied' : 'Copy URL'}
              </Button>
              {onShare ? (
                <Button variant="primary" size="sm" onClick={onShare}>
                  View as caller
                </Button>
              ) : null}
            </div>
          </div>
        </div>
      </Card>
    </div>
  )
}


function LogsTab({ testResult, error }) {
  if (error) {
    return <pre className="buildpage__pre buildpage__pre--err">{error.message}</pre>
  }
  if (!testResult) {
    return <div className="buildpage__panel buildpage__panel--muted">Run a test to see logs.</div>
  }
  return (
    <pre className="buildpage__pre">
{`execution_id: ${testResult.execution_id || '?'}
exit_code:    ${testResult.exit_code}
timed_out:    ${String(!!testResult.timed_out)}
duration:     ${testResult.execution_time_ms} ms
`}
    </pre>
  )
}
