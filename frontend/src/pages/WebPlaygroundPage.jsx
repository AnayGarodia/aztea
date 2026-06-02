import { useState } from 'react'

import { scrapeWeb, verifyWebReceipt } from '../api'
import Badge from '../ui/Badge'
import Button from '../ui/Button'
import Card from '../ui/Card'
import Input from '../ui/Input'
import MarkdownDoc from '../ui/MarkdownDoc'
import Pill from '../ui/Pill'
import Tabs from '../ui/Tabs'
import './WebPlaygroundPage.css'

// Pre-seeded so a first-time visitor's first interaction can never fail (design review #22).
const DEMO_URLS = ['https://example.com', 'https://news.ycombinator.com', 'https://www.python.org']


// The signed-receipt hook: verify provenance without re-crawling (the differentiator).
function ReceiptVerify({ receipt }) {
  const [state, setState] = useState('idle') // idle | checking | valid | invalid | error
  const [detail, setDetail] = useState(null)

  const run = async () => {
    setState('checking')
    setDetail(null)
    try {
      const res = await verifyWebReceipt(receipt)
      setDetail(res)
      setState(res?.valid ? 'valid' : 'invalid')
    } catch (err) {
      setState('error')
      setDetail({ note: err?.message || 'Verification failed.' })
    }
  }

  return (
    <Card>
      <Card.Body>
        <div className="web-pg__row-head">
          <span>🔏 Signed proof-of-observation</span>
          {state === 'valid' && <Badge variant="positive" label="Verified ✓" />}
          {state === 'invalid' && <Badge variant="negative" label="Does not verify" />}
          {state === 'error' && <Badge variant="warn" label="Error" />}
        </div>
        <p className="web-pg__muted">
          Provenance, not truth: a valid receipt proves this agent observed the page and
          signed it with its did:web key. Even Aztea can&apos;t forge it.
        </p>
        <dl className="web-pg__meta">
          <div><dt>Receipt</dt><dd><code>{receipt.receipt_id}</code></dd></div>
          <div><dt>Signer</dt><dd><code>{receipt.signer_did}</code></dd></div>
          <div><dt>Observed</dt><dd><code>{receipt.observation?.final_url}</code></dd></div>
        </dl>
        <Button size="sm" variant="secondary" loading={state === 'checking'} onClick={run}>
          {state === 'valid' || state === 'invalid' ? 'Re-verify' : 'Verify signature'}
        </Button>
        {detail?.note && <p className="web-pg__muted">{detail.note}</p>}
      </Card.Body>
    </Card>
  )
}


// "We turned this site into an API" — the API-discovery moment (design review #21).
function SiteApiCard({ apiSpec }) {
  if (!apiSpec || !apiSpec.endpoint_host) return null
  return (
    <Card variant="success">
      <Card.Body>
        <div className="web-pg__row-head">🔒 We compiled this site into an API</div>
        <p className="web-pg__muted">
          Found the JSON endpoint behind the page. Future calls replay it directly — no browser.
          The host is signed and non-templatable, so it can&apos;t be re-pointed.
        </p>
        <pre className="web-pg__code">{`${apiSpec.method || 'GET'} ${apiSpec.endpoint_host}`}</pre>
      </Card.Body>
    </Card>
  )
}


export default function WebPlaygroundPage() {
  const [url, setUrl] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [data, setData] = useState(null)
  const [ms, setMs] = useState(null)

  const scrape = async (target) => {
    const requested = (target || url).trim()
    if (!requested) return
    setUrl(requested)
    setLoading(true)
    setError(null)
    setData(null)
    const started = performance.now()
    try {
      const { status, body } = await scrapeWeb(requested, ['markdown', 'links'])
      setMs(Math.round(performance.now() - started))
      if (status === 503) {
        setError('The web API is disabled on this server. An operator sets AZTEA_WEB_API_ENABLED=1 to turn it on.')
        return
      }
      if (!body?.success) {
        setError(body?.error?.message || 'Could not scrape this URL.')
        return
      }
      setData(body.data)
    } catch (err) {
      setError(err?.message || 'Request failed.')
    } finally {
      setLoading(false)
    }
  }

  const apiSpec = data?.site_map?.api_spec
  const receipt = data?.observation_receipt
  const links = Array.isArray(data?.links) ? data.links : []

  return (
    <main className="web-pg">
      <header className="web-pg__hero">
        <h1>Paste a URL. Get clean data — and a signed receipt.</h1>
        <p className="web-pg__muted">
          Markdown, structured JSON, and the API hiding behind the page. Every result
          cryptographically signed and verifiable in one click.
        </p>
      </header>

      <Card>
        <Card.Body>
          <div className="web-pg__input-row">
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') scrape() }}
              placeholder="https://example.com"
              mono
            />
            <Button loading={loading} onClick={() => scrape()}>Scrape</Button>
          </div>
          <div className="web-pg__demos">
            <span className="web-pg__muted">Try:</span>
            {DEMO_URLS.map((demo) => (
              <Pill key={demo} interactive onClick={() => scrape(demo)}>
                {demo.replace('https://', '')}
              </Pill>
            ))}
          </div>
        </Card.Body>
      </Card>

      {loading && (
        <Card>
          <Card.Body>
            <p className="web-pg__muted">
              Fetching {url}… static pages return in ~1s; JS-heavy sites render headless and take a few seconds.
            </p>
          </Card.Body>
        </Card>
      )}

      {error && !loading && (
        <Card variant="danger">
          <Card.Body>
            <strong>Couldn&apos;t scrape this URL.</strong>
            <p className="web-pg__muted">{error}</p>
          </Card.Body>
        </Card>
      )}

      {data && !loading && (
        <div className="web-pg__results">
          <div className="web-pg__result-meta">
            {ms != null && <Badge variant="info" label={`${ms} ms`} />}
            {data.cost_class && (
              <Badge
                variant={data.cost_class === 'cheap' ? 'positive' : 'default'}
                label={data.cost_class === 'cheap' ? 'no browser' : 'rendered'}
              />
            )}
            {data.aztea_source && <Badge variant="default" label={data.aztea_source} />}
          </div>

          <SiteApiCard apiSpec={apiSpec} />
          {receipt && <ReceiptVerify receipt={receipt} />}

          <Tabs
            tabs={[
              { id: 'markdown', label: 'Markdown' },
              { id: 'json', label: 'Structured' },
              { id: 'links', label: `Links${links.length ? ` (${links.length})` : ''}` },
            ]}
            defaultTab="markdown"
          >
            {(active) => (
              active === 'markdown'
                ? (data.markdown
                    ? <MarkdownDoc content={data.markdown} />
                    : <p className="web-pg__muted">No markdown returned for this page.</p>)
                : active === 'json'
                  ? <pre className="web-pg__code">{JSON.stringify(data.json ?? null, null, 2)}</pre>
                  : (
                    <ul className="web-pg__links">
                      {links.slice(0, 200).map((href, i) => (
                        <li key={i}><a href={href} target="_blank" rel="noreferrer">{href}</a></li>
                      ))}
                    </ul>
                  )
            )}
          </Tabs>
        </div>
      )}
    </main>
  )
}
