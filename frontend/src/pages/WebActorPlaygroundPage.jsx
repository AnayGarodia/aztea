import { useState } from 'react'

import { scrapeWeb, verifyWebReceipt, webAct } from '../api'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Card from '../ui/Card'
import Input from '../ui/Input'
import MarkdownDoc from '../ui/MarkdownDoc'
import Pill from '../ui/Pill'
import Segmented from '../ui/Segmented'
import Select from '../ui/Select'
import './WebActorPlaygroundPage.css'

// One agent, two halves: READ (scrape) and ACT (interact / dry_run). The step
// vocabulary the action engine understands today — compare against Sisyphus to see
// where element-finding still lags.
const STEP_ACTIONS = ['click', 'fill', 'select', 'scroll', 'wait']
const TARGET_ACTIONS = new Set(['click', 'fill', 'select'])

const SCRAPE_PRESETS = ['https://news.ycombinator.com', 'https://www.python.org', 'https://example.com']
const ACTION_PRESETS = [
  { label: 'python.org — search', url: 'https://www.python.org',
    steps: [{ action: 'fill', target: 'Search', value: 'asyncio' }, { action: 'wait', target: '', value: '800' }] },
  { label: 'HN — scroll', url: 'https://news.ycombinator.com',
    steps: [{ action: 'scroll', target: '', value: '' }] },
]

const _emptyStep = () => ({ action: 'click', target: '', value: '' })


function errorMessage(status, body) {
  if (status === 503) {
    return 'This capability is disabled on the server. An operator sets AZTEA_WEB_API_ENABLED (read) / AZTEA_ACTION_WEB_ENABLED (act).'
  }
  const err = body?.error || body?.detail?.error
  return err?.message || 'The request failed.'
}


// The read differentiator: verify the signed observation receipt without re-crawling.
function ReceiptVerify({ receipt }) {
  const [state, setState] = useState('idle')
  const run = async () => {
    setState('checking')
    try {
      const r = await verifyWebReceipt(receipt)
      setState(r?.valid ? 'valid' : 'invalid')
    } catch {
      setState('error')
    }
  }
  return (
    <Card>
      <Card.Body>
        <div className="web-act__row-head">
          🔏 Signed proof-of-observation
          {state === 'valid' && <Badge variant="positive" label="Verified ✓" />}
          {state === 'invalid' && <Badge variant="negative" label="Does not verify" />}
          {state === 'error' && <Badge variant="warn" label="Error" />}
        </div>
        <p className="web-act__muted">Signer: <code>{receipt.signer_did}</code></p>
        <Button size="sm" variant="secondary" loading={state === 'checking'} onClick={run}>
          Verify signature
        </Button>
      </Card.Body>
    </Card>
  )
}


export default function WebActorPlaygroundPage() {
  const [mode, setMode] = useState('scrape')   // scrape | interact | dry_run
  const [url, setUrl] = useState('')
  const [steps, setSteps] = useState([_emptyStep()])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [data, setData] = useState(null)   // scrape result
  const [act, setAct] = useState(null)     // action envelope
  const [ms, setMs] = useState(null)
  const [followLinks, setFollowLinks] = useState(0)  // scrape: read top-N linked articles

  const isAction = mode === 'interact' || mode === 'dry_run'

  const setStep = (i, patch) => setSteps((s) => s.map((st, j) => (j === i ? { ...st, ...patch } : st)))
  const addStep = () => setSteps((s) => [...s, _emptyStep()])
  const removeStep = (i) => setSteps((s) => (s.length > 1 ? s.filter((_, j) => j !== i) : s))
  const loadActionPreset = (p) => { setUrl(p.url); setSteps(p.steps.map((x) => ({ ...x }))) }

  const run = async () => {
    const target = url.trim()
    if (!target) return
    setLoading(true); setError(null); setData(null); setAct(null)
    const started = performance.now()
    try {
      if (mode === 'scrape') {
        const { status, body } = await scrapeWeb(target, ['markdown', 'links'], followLinks)
        setMs(Math.round(performance.now() - started))
        if (status === 503 || !body?.success) {
          setError(status === 503 ? errorMessage(503) : (body?.error?.message || 'Could not scrape this URL.'))
          return
        }
        setData(body.data)
      } else {
        const cleaned = steps
          .filter((s) => s.action && (!TARGET_ACTIONS.has(s.action) || s.target.trim()))
          .map((s) => ({ action: s.action, target: s.target.trim(), value: s.value.trim() }))
        const { status, body } = await webAct({ action: mode, url: target, steps: cleaned })
        setMs(Math.round(performance.now() - started))
        if (status === 503 || body?.error || body?.detail?.error) {
          setError(errorMessage(status, body))
          return
        }
        setAct(body)
      }
    } catch (err) {
      setError(err?.message || 'Request failed.')
    } finally {
      setLoading(false)
    }
  }

  const revealed = mode === 'dry_run' ? act?.revealed : act
  const planned = act?.planned
  const links = Array.isArray(data?.links) ? data.links : []
  const receipt = data?.observation_receipt
  // Followed articles: lead with the first one that actually has readable text (some
  // pages are JS/image-heavy and come back near-empty), and flag the thin ones.
  const linkedPages = Array.isArray(data?.linked_pages) ? data.linked_pages : []
  const THIN_ARTICLE_CHARS = 400
  const openArticleIdx = Math.max(0, linkedPages.findIndex((p) => (p.markdown || '').length > THIN_ARTICLE_CHARS))

  return (
    <main className="web-act">
      <header className="web-act__hero">
        <h1>Web Agent</h1>
        <p className="web-act__muted">
          One agent that <strong>reads</strong> and <strong>acts</strong> on the live web.
          <strong> Scrape</strong> a page to markdown + a signed receipt;
          <strong> interact</strong> to drive it (click / fill / select / scroll); or
          <strong> dry-run</strong> to see what a commit would do without acting.
          Watch the step results to find where element-finding lags.
        </p>
      </header>

      <Card>
        <Card.Body>
          <Segmented
            value={mode}
            onChange={(m) => { setMode(m); setData(null); setAct(null); setError(null) }}
            options={[
              { value: 'scrape', label: 'Scrape' },
              { value: 'interact', label: 'Interact' },
              { value: 'dry_run', label: 'Dry-run' },
            ]}
          />
          <div className="web-act__input-row">
            <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com" mono />
            <Button loading={loading} onClick={run}>Run</Button>
          </div>
          <div className="web-act__presets">
            <span className="web-act__muted">Presets:</span>
            {mode === 'scrape'
              ? SCRAPE_PRESETS.map((u) => (
                  <Pill key={u} interactive onClick={() => setUrl(u)}>{u.replace('https://', '')}</Pill>
                ))
              : ACTION_PRESETS.map((p) => (
                  <Pill key={p.label} interactive onClick={() => loadActionPreset(p)}>{p.label}</Pill>
                ))}
          </div>

          {mode === 'scrape' && (
            <div className="web-act__follow">
              <span className="web-act__muted">Read the linked articles (follow the story links):</span>
              <Select value={followLinks} onChange={(e) => setFollowLinks(Number(e.target.value))}>
                <option value={0}>just this page (headlines)</option>
                <option value={5}>read top 5 articles</option>
                <option value={10}>read top 10 articles</option>
                <option value={30}>read all articles (up to 30)</option>
              </Select>
            </div>
          )}

          {isAction && (
            <div className="web-act__steps">
              {steps.map((s, i) => (
                <div className="web-act__step" key={i}>
                  <span className="web-act__step-n">{i + 1}</span>
                  <Select value={s.action} onChange={(e) => setStep(i, { action: e.target.value })}>
                    {STEP_ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                  </Select>
                  <Input
                    value={s.target}
                    onChange={(e) => setStep(i, { target: e.target.value })}
                    placeholder={TARGET_ACTIONS.has(s.action) ? 'target (visible text / label)' : '—'}
                    disabled={!TARGET_ACTIONS.has(s.action)}
                  />
                  <Input
                    value={s.value}
                    onChange={(e) => setStep(i, { value: e.target.value })}
                    placeholder={s.action === 'wait' ? 'ms' : 'value'}
                  />
                  <Button size="sm" variant="ghost" onClick={() => removeStep(i)}>✕</Button>
                </div>
              ))}
              <Button size="sm" variant="secondary" onClick={addStep}>+ Add step</Button>
            </div>
          )}
        </Card.Body>
      </Card>

      {error && !loading && (
        <Card variant="danger">
          <Card.Body>
            <strong>Couldn&apos;t complete that.</strong>
            <p className="web-act__muted">{error}</p>
          </Card.Body>
        </Card>
      )}

      {/* ── Scrape (read) results ── */}
      {data && !loading && (
        <div className="web-act__results">
          <div className="web-act__result-meta">
            {ms != null && <Badge variant="info" label={`${ms} ms`} />}
            {data.cost_class && (
              <Badge
                variant={data.cost_class === 'cheap' ? 'positive' : 'default'}
                label={data.cost_class === 'cheap' ? 'no browser' : 'rendered'}
              />
            )}
            {data.aztea_source && <Badge variant="default" label={data.aztea_source} />}
            <Badge variant="default" label={`links ${links.length}`} />
          </div>
          {receipt && <ReceiptVerify receipt={receipt} />}
          {/* When articles were read, LEAD with them — that's the content the user wants.
              Auto-open the first article with real text; flag the thin (JS/image) ones. */}
          {linkedPages.length > 0 && (
            <Card>
              <Card.Body>
                <div className="web-act__row-head">📄 Articles read ({linkedPages.length})</div>
                {linkedPages.map((p, i) => {
                  const len = (p.markdown || '').trim().length
                  return (
                    <details key={i} className="web-act__article" open={i === openArticleIdx}>
                      <summary>
                        {p.title || p.url}
                        {len <= THIN_ARTICLE_CHARS && <span className="web-act__muted"> · little text</span>}
                      </summary>
                      <p className="web-act__muted">
                        <a href={p.url} target="_blank" rel="noreferrer">{p.url}</a>
                      </p>
                      {len > 0
                        ? <MarkdownDoc content={p.markdown} />
                        : <p className="web-act__muted">Little readable text extracted — likely a JS- or image-heavy page.</p>}
                    </details>
                  )
                })}
              </Card.Body>
            </Card>
          )}

          {/* The page itself. Demote the headline index to a collapsed block once we've
              read the articles, so it's available but not the dominant output. */}
          <Card>
            <Card.Body>
              {data.linked_pages?.length ? (
                <details className="web-act__article">
                  <summary>Page index (headlines)</summary>
                  {data.markdown
                    ? <MarkdownDoc content={data.markdown} />
                    : <p className="web-act__muted">No markdown returned for this page.</p>}
                </details>
              ) : (
                data.markdown
                  ? <MarkdownDoc content={data.markdown} />
                  : <p className="web-act__muted">No markdown returned for this page.</p>
              )}
            </Card.Body>
          </Card>
        </div>
      )}

      {/* ── Interact / dry-run (act) results ── */}
      {act && !loading && (
        <div className="web-act__results">
          <div className="web-act__result-meta">
            {ms != null && <Badge variant="info" label={`${ms} ms`} />}
            <Badge variant="default" label={act.phase || 'done'} />
            {revealed && (
              <Badge
                variant={revealed.steps_completed === revealed.steps_total ? 'positive' : 'warn'}
                label={`steps ${revealed.steps_completed ?? 0}/${revealed.steps_total ?? 0}`}
              />
            )}
          </div>
          {planned && (
            <Card variant="success">
              <Card.Body>
                <div className="web-act__row-head">🧾 What a commit would be authorized to do</div>
                <pre className="web-act__code">{JSON.stringify(planned, null, 2)}</pre>
                <p className="web-act__muted">Dry-run only: nothing was committed and the mandate was not consumed.</p>
              </Card.Body>
            </Card>
          )}
          {revealed && (
            <Card>
              <Card.Body>
                <div className="web-act__row-head">{revealed.title || 'Revealed page'}</div>
                <p className="web-act__muted">{revealed.final_url}</p>
                <pre className="web-act__code">{(revealed.text || '(no text revealed)').slice(0, 4000)}</pre>
              </Card.Body>
            </Card>
          )}
        </div>
      )}
    </main>
  )
}
