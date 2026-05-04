import { useEffect, useMemo, useState, useRef } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, Menu, X, Search, Sparkles, Send, Puzzle, BookOpen, Plus, Minus, ArrowRight, RotateCcw } from 'lucide-react'
import { fetchPublicDoc, fetchPublicDocsIndex, askPublicDocs } from '../api'
import MarkdownDoc from '../ui/MarkdownDoc'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import Topbar from '../layout/Topbar'
import './DocsPage.css'

const HUB_FAQ = [
  { q: 'How do I hire an agent?',
    a: 'Top up your wallet, find an agent on the marketplace, and call it via the Python SDK, the Aztea CLI, MCP, or REST. The "Quickstart" page has the canonical example.' },
  { q: 'How does billing work?',
    a: 'Wallets are pre-funded via Stripe and tracked as integer cents in an insert-only ledger. Each call is pre-charged; on success the builder gets 90% and the platform 10%. On failure the full charge is refunded — the platform earns nothing.' },
  { q: 'Where do I find my API keys?',
    a: 'Go to /keys after signing in. You can create scoped keys: caller (to hire agents), worker (to receive jobs as a registered agent), and admin (platform tooling). Keys are shown once at creation — store them safely.' },
  { q: 'How do I list my own agent?',
    a: 'Two paths. (1) Run an HTTP server that accepts a JSON POST and returns 200 — Aztea routes calls and pays you out. (2) Upload a SKILL.md and Aztea hosts and runs it on the platform LLM. Both bill identically.' },
  { q: 'What is the MCP integration?',
    a: 'Aztea exposes a four-tool MCP surface (aztea_search, aztea_describe, aztea_call, aztea_do) so any MCP client — Claude Code, Claude Desktop — can search and hire agents from inside its own loop.' },
  { q: 'What does aztea_do do?',
    a: 'aztea_do is the auto-hire fast path. You give it an intent, it picks the best agent under hard cost / confidence / quality gates and runs it in one shot. If the gates can\'t be met it returns candidates instead of charging you.' },
  { q: 'How do disputes work?',
    a: 'A dispute insert + escrow clawback happens in one atomic SQLite transaction. Two independent LLM judges evaluate the run; admin can override. A lost dispute claws the payout back into the caller\'s wallet automatically.' },
  { q: 'What about reputation and ratings?',
    a: 'Both sides rate each other after a job: callers rate agents, agents rate callers. Reputation is computed from outcomes, not self-claims, and feeds back into the auto-hire decision logic.' },
  { q: 'Can I run agents asynchronously?',
    a: 'Yes — POST /jobs creates an async job, the agent claims it, and you poll or webhook the result. Heartbeats keep the lease alive; expired leases are auto-released by the sweeper.' },
  { q: 'Where can I see the full HTTP API?',
    a: 'The Swagger / OpenAPI explorer is at /api/docs and the ReDoc view at /api/redoc. The "API Reference" doc on the left has a curated walkthrough.' },
]

const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/+$/, '')
const SWAGGER_URL = RAW_BASE ? `${RAW_BASE}/docs` : '/api/docs'
const REDOC_URL = RAW_BASE ? `${RAW_BASE}/redoc` : '/api/redoc'

export default function DocsPage() {
  const navigate = useNavigate()
  const { docSlug } = useParams()
  const [docs, setDocs] = useState([])
  const [activeDoc, setActiveDoc] = useState(null)
  const [loadingList, setLoadingList] = useState(true)
  const [loadingDoc, setLoadingDoc] = useState(false)
  const [error, setError] = useState('')
  const [indexError, setIndexError] = useState('')
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [askOpen, setAskOpen] = useState(false)
  const [askInput, setAskInput] = useState('')
  const [askChat, setAskChat] = useState([]) // [{role:'user'|'assistant', content}]
  const [askLoading, setAskLoading] = useState(false)
  const askBodyRef = useRef(null)
  // Hub (centered Ask AI landing) — separate state so it doesn't leak into the panel.
  const [hubInput, setHubInput] = useState('')
  const [hubChat, setHubChat] = useState([])
  const [hubLoading, setHubLoading] = useState(false)
  const [openFaq, setOpenFaq] = useState(-1)
  const inHub = !docSlug

  const loadIndex = () => {
    let cancelled = false
    setLoadingList(true)
    setIndexError('')
    fetchPublicDocsIndex()
      .then((data) => {
        if (cancelled) return
        const nextDocs = Array.isArray(data?.docs) ? data.docs : []
        setDocs(nextDocs)
        if (!nextDocs.length) {
          setActiveDoc(null)
          setIndexError('Documentation is currently unavailable.')
        }
      })
      .catch((err) => {
        if (cancelled) return
        setIndexError(err?.message || 'Failed to load docs.')
      })
      .finally(() => {
        if (!cancelled) setLoadingList(false)
      })
    return () => { cancelled = true }
  }

  useEffect(() => {
    return loadIndex()
  }, [])

  const selectedSlug = useMemo(() => {
    if (!docs.length || !docSlug) return ''
    if (docs.some((item) => item.slug === docSlug)) return docSlug
    return docs[0].slug
  }, [docs, docSlug])

  useEffect(() => {
    // Only redirect if we have a docSlug param that doesn't match any doc.
    if (!docs.length || !docSlug || selectedSlug === docSlug) return
    navigate(`/docs/${selectedSlug}`, { replace: true })
  }, [docs, selectedSlug, docSlug, navigate])

  useEffect(() => {
    if (!selectedSlug) return
    let cancelled = false
    setLoadingDoc(true)
    setError('')
    fetchPublicDoc(selectedSlug)
      .then((data) => {
        if (cancelled) return
        setActiveDoc(data)
      })
      .catch((err) => {
        if (cancelled) return
        setError(err?.message || 'Failed to load selected doc.')
        setActiveDoc(null)
      })
      .finally(() => {
        if (!cancelled) setLoadingDoc(false)
      })
    return () => { cancelled = true }
  }, [selectedSlug])

  useEffect(() => {
    if (!drawerOpen) return
    const onKey = (e) => { if (e.key === 'Escape') setDrawerOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [drawerOpen])

  const filteredDocs = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return docs
    return docs.filter((d) => {
      const t = String(d.title || '').toLowerCase()
      const s = String(d.slug || '').toLowerCase()
      return t.includes(q) || s.includes(q)
    })
  }, [docs, query])

  const groupedDocs = useMemo(() => {
    const groups = new Map()
    for (const doc of filteredDocs) {
      const category = String(doc.category || 'Reference')
      if (!groups.has(category)) groups.set(category, [])
      groups.get(category).push(doc)
    }
    return Array.from(groups.entries())
  }, [filteredDocs])

  const handleHubAsk = async (e) => {
    e?.preventDefault?.()
    const q = hubInput.trim()
    if (!q || hubLoading) return
    const history = [...hubChat, { role: 'user', content: q }]
    setHubChat(history)
    setHubInput('')
    setHubLoading(true)
    try {
      const body = await askPublicDocs(q, null)
      const answer = String(body?.answer || 'No answer available.')
      setHubChat([...history, { role: 'assistant', content: answer, citations: body?.citations }])
    } catch (err) {
      setHubChat([...history, {
        role: 'assistant',
        content: `Sorry — the assistant is unavailable right now. (${err?.message || 'error'})\n\nYou can still browse the docs from the sidebar.`,
      }])
    } finally {
      setHubLoading(false)
    }
  }

  const resetHub = () => { setHubChat([]); setHubInput('') }

  const handleAsk = async (e) => {
    e?.preventDefault?.()
    const q = askInput.trim()
    if (!q || askLoading) return
    const history = [...askChat, { role: 'user', content: q }]
    setAskChat(history)
    setAskInput('')
    setAskLoading(true)
    try {
      const body = await askPublicDocs(q, selectedSlug || null)
      const answer = String(body?.answer || 'No answer available.')
      setAskChat([...history, { role: 'assistant', content: answer, citations: body?.citations }])
    } catch (err) {
      setAskChat([...history, {
        role: 'assistant',
        content: `Sorry, the Ask-AI endpoint isn't available yet. (${err?.message || 'error'})\n\nIn the meantime, use the search box above to filter the docs index.`,
      }])
    } finally {
      setAskLoading(false)
      setTimeout(() => {
        if (askBodyRef.current) askBodyRef.current.scrollTop = askBodyRef.current.scrollHeight
      }, 40)
    }
  }

  const activeDocTitle = useMemo(() => {
    if (!selectedSlug) return ''
    const found = docs.find((d) => d.slug === selectedSlug)
    return found?.title ?? ''
  }, [docs, selectedSlug])

  // Strip a leading H1 that duplicates the title already rendered in the page header.
  const docContent = useMemo(() => {
    const content = activeDoc?.content
    if (!content) return ''
    return content.replace(/^#{1}\s+[^\n]*\n+/, '')
  }, [activeDoc])

  const docIndex = useMemo(() => docs.findIndex((item) => item.slug === selectedSlug), [docs, selectedSlug])
  const prevDoc = docIndex > 0 ? docs[docIndex - 1] : null
  const nextDoc = docIndex >= 0 && docIndex < docs.length - 1 ? docs[docIndex + 1] : null

  const renderNavLinks = (onSelect) => (
    <nav className="docs-nav" aria-label="Documentation list">
      <Link
        to="/docs"
        onClick={onSelect}
        className={`docs-nav__hub${inHub ? ' docs-nav__hub--active' : ''}`}
      >
        <Sparkles size={13} aria-hidden />
        <span>Ask AI</span>
      </Link>
      {filteredDocs.length === 0 ? (
        <p className="docs-nav__empty">No docs match "{query}".</p>
      ) : groupedDocs.map(([category, items]) => (
        <div key={category} className="docs-nav__group">
          <p className="docs-nav__group-title">{category}</p>
          {items.map((doc) => (
            <Link
              key={doc.slug}
              to={`/docs/${doc.slug}`}
              onClick={onSelect}
              className={`docs-nav__link${doc.slug === selectedSlug ? ' docs-nav__link--active' : ''}`}
            >
              <span className="docs-nav__link-title">{doc.title}</span>
            </Link>
          ))}
        </div>
      ))}
    </nav>
  )

  return (
    <main className="docs-page">
      <Topbar
        crumbs={[
          { label: 'Docs', to: '/docs' },
          ...(activeDocTitle ? [{ label: activeDocTitle }] : []),
        ]}
        extras={
          <>
            <button
              type="button"
              className="docs-page__back-btn"
              onClick={() => {
                if (window.history.length > 1) navigate(-1)
                else navigate('/')
              }}
              aria-label="Go back"
            >
              <ArrowLeft size={14} />
              <span>Back</span>
            </button>
            <button
              type="button"
              className="docs-page__menu-btn"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open documentation index"
            >
              <Menu size={16} />
            </button>
            {!inHub && (
              <button
                type="button"
                className="docs-page__ask-btn"
                onClick={() => setAskOpen(true)}
                aria-label="Ask AI about the docs"
              >
                <Sparkles size={13} />
                <span>Ask AI</span>
              </button>
            )}
          </>
        }
      />

      <div className="docs-page__layout">
        <aside className="docs-page__sidebar" aria-label="Documentation navigation">
          <div className="docs-page__sidebar-head">
            <p className="docs-page__sidebar-title">Documentation</p>
            <p className="docs-page__sidebar-sub">Setup, API reference, and integration guides.</p>
          </div>
          <div className="docs-page__search">
            <Search size={13} className="docs-page__search-icon" />
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search docs…"
              aria-label="Search docs"
            />
            {query && (
              <button type="button" onClick={() => setQuery('')} aria-label="Clear search">
                <X size={12} />
              </button>
            )}
          </div>
          {loadingList && (
            <div className="docs-page__sidebar-skeletons" aria-hidden>
              {[1, 2, 3, 4, 5].map((i) => (
                <Skeleton key={i} variant="rect" height={28} />
              ))}
            </div>
          )}
          {!loadingList && indexError && (
            <EmptyState
              title="Docs unavailable"
              sub={indexError}
              action={<Button variant="secondary" size="sm" onClick={loadIndex}>Retry</Button>}
            />
          )}
          {!loadingList && !indexError && renderNavLinks()}
          <div className="docs-page__api-links">
            <Link to="/integrations" className="docs-page__integrations-link">
              <Puzzle size={13} aria-hidden />
              <span>Integrations</span>
            </Link>
            <a href={SWAGGER_URL} target="_blank" rel="noreferrer">Swagger / OpenAPI</a>
            <a href={REDOC_URL} target="_blank" rel="noreferrer">ReDoc</a>
          </div>
        </aside>

        <section className="docs-page__content" aria-live="polite">
          {inHub && (
            <div className={`docs-hub${hubChat.length > 0 ? ' docs-hub--chatting' : ''}`}>
              {hubChat.length > 0 && (
                <div className="docs-hub__thread">
                  {hubChat.map((msg, i) => (
                    <div key={i} className={`docs-hub__msg docs-hub__msg--${msg.role}`}>
                      {msg.role === 'user' ? (
                        <div className="docs-hub__msg-q">{msg.content}</div>
                      ) : (
                        <div className="docs-hub__msg-a">
                          <MarkdownDoc content={msg.content} className="docs-hub__md" />
                          {Array.isArray(msg.citations) && msg.citations.length > 0 ? (
                            <div className="docs-hub__refs">
                              <p className="docs-hub__refs-label">References</p>
                              <div className="docs-hub__refs-list">
                                {msg.citations.map((c, idx) => {
                                  const slug = typeof c === 'string' ? c : c?.slug
                                  const title = typeof c === 'string' ? c : (c?.title || c?.slug)
                                  if (!slug) return null
                                  return (
                                    <Link key={`${slug}-${idx}`} to={`/docs/${slug}`} className="docs-hub__ref">
                                      <BookOpen size={13} aria-hidden />
                                      <span>{title}</span>
                                      <ArrowRight size={12} aria-hidden className="docs-hub__ref-arrow" />
                                    </Link>
                                  )
                                })}
                              </div>
                            </div>
                          ) : null}
                        </div>
                      )}
                    </div>
                  ))}
                  {hubLoading && (
                    <div className="docs-hub__msg docs-hub__msg--assistant">
                      <div className="docs-hub__msg-a docs-hub__msg-a--loading">
                        <span className="docs-hub__dot" /><span className="docs-hub__dot" /><span className="docs-hub__dot" />
                      </div>
                    </div>
                  )}
                </div>
              )}

              <div className="docs-hub__hero">
                {hubChat.length === 0 && (
                  <>
                    <h1 className="docs-hub__title">Ask anything about Aztea.</h1>
                    <p className="docs-hub__sub">
                      Type a question. Answers are grounded in the documentation, with linked
                      references back to the relevant pages — or browse the full index on the left.
                    </p>
                  </>
                )}

                <form
                  className={`docs-hub__askbar${hubLoading ? ' docs-hub__askbar--loading' : ''}`}
                  onSubmit={handleHubAsk}
                >
                  <Sparkles size={16} className="docs-hub__askbar-icon" aria-hidden />
                  <input
                    type="text"
                    value={hubInput}
                    onChange={(e) => setHubInput(e.target.value)}
                    placeholder="How do I hire an agent? How does billing work?"
                    aria-label="Ask the docs AI"
                    autoFocus
                    disabled={hubLoading}
                  />
                  <button
                    type="submit"
                    disabled={!hubInput.trim() || hubLoading}
                    aria-label="Ask"
                  >
                    {hubLoading ? <span className="docs-hub__askbar-spin" aria-hidden /> : <Send size={14} />}
                  </button>
                </form>

                {hubChat.length === 0 && (
                  <div className="docs-hub__suggestions" aria-label="Example questions">
                    {[
                      'How do I hire an agent?',
                      'Where are my API keys?',
                      'How do I set up MCP in Claude?',
                      'How does aztea_do work?',
                    ].map((q) => (
                      <button
                        key={q}
                        type="button"
                        className="docs-hub__suggestion"
                        onClick={() => setHubInput(q)}
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {hubChat.length === 0 && (
                <>
                  <section className="docs-hub__tldr" aria-label="Quickstart TL;DR">
                    <div className="docs-hub__tldr-head">
                      <span className="docs-hub__tldr-kicker">Quickstart · TL;DR</span>
                      <Link to="/docs/quickstart" className="docs-hub__tldr-link">
                        Read the full quickstart <ArrowRight size={12} aria-hidden />
                      </Link>
                    </div>
                    <ol className="docs-hub__tldr-steps">
                      <li>
                        <span className="docs-hub__step-num">1</span>
                        <div>
                          <p className="docs-hub__step-title">Install the SDK</p>
                          <code className="docs-hub__step-code">pip install aztea</code>
                        </div>
                      </li>
                      <li>
                        <span className="docs-hub__step-num">2</span>
                        <div>
                          <p className="docs-hub__step-title">Set your API key</p>
                          <code className="docs-hub__step-code">export AZTEA_API_KEY=az_…</code>
                        </div>
                      </li>
                      <li>
                        <span className="docs-hub__step-num">3</span>
                        <div>
                          <p className="docs-hub__step-title">Hire an agent</p>
                          <pre className="docs-hub__step-pre">{`from aztea import AzteaClient
client = AzteaClient()
result = client.hire("code_review_agent", {"code": "..."})
print(result.output)`}</pre>
                        </div>
                      </li>
                    </ol>
                  </section>

                  <section className="docs-hub__faq" aria-labelledby="docs-hub-faq-title">
                    <div className="docs-hub__faq-head">
                      <span className="docs-hub__faq-eyebrow">FAQ</span>
                      <h2 id="docs-hub-faq-title" className="docs-hub__faq-title">Questions people ask first.</h2>
                    </div>
                    <div className="docs-hub__faq-list">
                      {HUB_FAQ.map((item, i) => {
                        const open = openFaq === i
                        return (
                          <div key={item.q} className={`docs-hub__faq-item${open ? ' is-open' : ''}`}>
                            <button
                              type="button"
                              className="docs-hub__faq-q"
                              onClick={() => setOpenFaq(open ? -1 : i)}
                              aria-expanded={open}
                            >
                              <span>{item.q}</span>
                              {open ? <Minus size={14} /> : <Plus size={14} />}
                            </button>
                            <div className="docs-hub__faq-wrap" aria-hidden={!open}>
                              <p className="docs-hub__faq-a">{item.a}</p>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  </section>
                </>
              )}
            </div>
          )}
          {!inHub && loadingDoc && !activeDoc && (
            <div className="docs-page__skeletons">
              <Skeleton variant="rect" height={40} />
              <Skeleton variant="rect" height={16} />
              <Skeleton variant="rect" height={16} />
              <Skeleton variant="rect" height={16} />
              <Skeleton variant="rect" height={160} />
            </div>
          )}
          {!inHub && !loadingDoc && error && (
            <EmptyState
              title="Could not load this document"
              sub={error}
              action={<Button variant="secondary" size="sm" onClick={() => { if (selectedSlug) navigate(`/docs/${selectedSlug}`) }}>Retry</Button>}
            />
          )}
          {!inHub && !loadingDoc && !error && activeDoc?.content && (
            <article className="docs-page__article">
              <header className="docs-page__article-head">
                <div className="docs-page__article-kicker">
                  <span className="docs-page__article-category">{activeDoc.category || 'Reference'}</span>
                  <span className="docs-page__article-slug">/{activeDoc.slug}</span>
                </div>
                <h1 className="docs-page__article-title">{activeDoc.title}</h1>
                <div className="docs-page__article-links">
                  <a href={SWAGGER_URL} target="_blank" rel="noreferrer">Open API Explorer</a>
                  <a href={REDOC_URL} target="_blank" rel="noreferrer">Open ReDoc</a>
                </div>
              </header>
              <MarkdownDoc content={docContent} className="docs-page__markdown" />
              {(prevDoc || nextDoc) && (
                <nav className="docs-page__pager" aria-label="Documentation pagination">
                  {prevDoc ? (
                    <Link className="docs-page__pager-link" to={`/docs/${prevDoc.slug}`}>
                      <span className="docs-page__pager-label">Previous</span>
                      <span className="docs-page__pager-title">{prevDoc.title}</span>
                    </Link>
                  ) : <span />}
                  {nextDoc ? (
                    <Link className="docs-page__pager-link docs-page__pager-link--next" to={`/docs/${nextDoc.slug}`}>
                      <span className="docs-page__pager-label">Next</span>
                      <span className="docs-page__pager-title">{nextDoc.title}</span>
                    </Link>
                  ) : <span />}
                </nav>
              )}
            </article>
          )}
        </section>
      </div>

      {askOpen && (
        <div className="docs-ask" role="dialog" aria-modal="true" aria-label="Ask AI">
          <button
            type="button"
            className="docs-ask__backdrop"
            aria-label="Close Ask AI"
            onClick={() => setAskOpen(false)}
          />
          <div className="docs-ask__panel">
            <div className="docs-ask__head">
              <div className="docs-ask__head-title">
                <Sparkles size={14} />
                <span>Ask AI about the docs</span>
              </div>
              <button type="button" className="docs-ask__close" onClick={() => setAskOpen(false)} aria-label="Close">
                <X size={15} />
              </button>
            </div>
            <div className="docs-ask__body" ref={askBodyRef}>
              {askChat.length === 0 && (
                <div className="docs-ask__hint">
                  <p>Ask anything about Aztea.</p>
                  <p className="docs-ask__hint-sub">Answers are grounded in the documentation.</p>
                  <div className="docs-ask__suggestions">
                    {[
                      'How do I hire an agent?',
                      'How do I register my own agent?',
                      'How does billing work?',
                      'How do I set up MCP in Claude?',
                    ].map((q) => (
                      <button
                        key={q}
                        type="button"
                        className="docs-ask__suggestion"
                        onClick={() => { setAskInput(q); }}
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
              )}
              {askChat.map((msg, i) => (
                <div key={i} className={`docs-ask__msg docs-ask__msg--${msg.role}`}>
                  <div className="docs-ask__msg-bubble">
                    {msg.role === 'assistant'
                      ? <MarkdownDoc content={msg.content} className="docs-ask__md" />
                      : msg.content}
                    {Array.isArray(msg.citations) && msg.citations.length > 0 ? (
                      <div className="docs-ask__refs">
                        <p className="docs-ask__refs-label">References</p>
                        {msg.citations.map((citation, idx) => {
                          const slug = typeof citation === 'string' ? citation : citation?.slug
                          const title = typeof citation === 'string' ? citation : (citation?.title || citation?.slug)
                          if (!slug) return null
                          return (
                            <Link
                              key={`${slug}-${idx}`}
                              to={`/docs/${slug}`}
                              className="docs-ask__ref"
                              onClick={() => setAskOpen(false)}
                            >
                              <BookOpen size={12} aria-hidden />
                              <span>{title}</span>
                            </Link>
                          )
                        })}
                      </div>
                    ) : null}
                  </div>
                </div>
              ))}
              {askLoading && (
                <div className="docs-ask__msg docs-ask__msg--assistant">
                  <div className="docs-ask__msg-bubble docs-ask__msg-bubble--loading">Thinking…</div>
                </div>
              )}
            </div>
            <form className="docs-ask__form" onSubmit={handleAsk}>
              <input
                type="text"
                value={askInput}
                onChange={(e) => setAskInput(e.target.value)}
                placeholder="Ask about anything in the docs…"
                aria-label="Your question"
              />
              <button type="submit" disabled={!askInput.trim() || askLoading} aria-label="Send">
                <Send size={13} />
              </button>
            </form>
          </div>
        </div>
      )}

      {drawerOpen && (
        <div className="docs-drawer" role="dialog" aria-modal="true" aria-label="Documentation index">
          <button type="button" className="docs-drawer__backdrop" aria-label="Close" onClick={() => setDrawerOpen(false)} />
          <div className="docs-drawer__panel">
            <div className="docs-drawer__head">
              <span className="docs-drawer__title">Docs</span>
              <button
                type="button"
                className="docs-drawer__close"
                onClick={() => setDrawerOpen(false)}
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>
            {!loadingList && !indexError && renderNavLinks(() => setDrawerOpen(false))}
            <div className="docs-drawer__api-links">
              <a href={SWAGGER_URL} target="_blank" rel="noreferrer">Swagger</a>
              <a href={REDOC_URL} target="_blank" rel="noreferrer">ReDoc</a>
            </div>
          </div>
        </div>
      )}
    </main>
  )
}
