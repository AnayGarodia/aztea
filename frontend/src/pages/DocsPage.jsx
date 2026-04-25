import { useEffect, useMemo, useState, useRef } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Moon, Sun, Menu, X, ArrowLeft, Search, Sparkles, Send } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useTheme } from '../context/ThemeContext'
import { fetchPublicDoc, fetchPublicDocsIndex } from '../api'
import MarkdownDoc from '../ui/MarkdownDoc'
import Button from '../ui/Button'
import Skeleton from '../ui/Skeleton'
import EmptyState from '../ui/EmptyState'
import './DocsPage.css'

const RAW_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/+$/, '')
const SWAGGER_URL = RAW_BASE ? `${RAW_BASE}/docs` : '/api/docs'
const REDOC_URL = RAW_BASE ? `${RAW_BASE}/redoc` : '/api/redoc'

export default function DocsPage() {
  const navigate = useNavigate()
  const { docSlug } = useParams()
  const { isDark, toggle: toggleTheme } = useTheme()
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
    if (!docs.length) return ''
    if (docSlug && docs.some((item) => item.slug === docSlug)) return docSlug
    return docs[0].slug
  }, [docs, docSlug])

  useEffect(() => {
    if (!docs.length || selectedSlug === docSlug) return
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

  const handleAsk = async (e) => {
    e?.preventDefault?.()
    const q = askInput.trim()
    if (!q || askLoading) return
    const history = [...askChat, { role: 'user', content: q }]
    setAskChat(history)
    setAskInput('')
    setAskLoading(true)
    try {
      // Uses the same backend request helper — kept inline to avoid circular imports.
      const url = `${RAW_BASE}/public/docs/ask`
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question: q, doc_slug: selectedSlug || null }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const body = await res.json()
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

  const renderNavLinks = (onSelect) => (
    <nav className="docs-nav" aria-label="Documentation list">
      {filteredDocs.length === 0 ? (
        <p className="docs-nav__empty">No docs match "{query}".</p>
      ) : filteredDocs.map((doc) => (
        <Link
          key={doc.slug}
          to={`/docs/${doc.slug}`}
          onClick={onSelect}
          className={`docs-nav__link${doc.slug === selectedSlug ? ' docs-nav__link--active' : ''}`}
        >
          {doc.title}
        </Link>
      ))}
    </nav>
  )

  return (
    <main className="docs-page">
      <header className="docs-page__topbar">
        <div className="docs-page__topbar-left">
          <button
            type="button"
            className="docs-page__menu-btn"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open documentation index"
          >
            <Menu size={16} />
          </button>
          <Link to="/" className="docs-page__home">
            <ArrowLeft size={14} aria-hidden />
            <span className="docs-page__home-label">Home</span>
          </Link>
          <span className="docs-page__topbar-sep" aria-hidden>·</span>
          <span className="docs-page__topbar-title">Docs{activeDocTitle ? <span className="docs-page__topbar-sub"> / {activeDocTitle}</span> : null}</span>
        </div>
        <div className="docs-page__topbar-right">
          <button
            type="button"
            className="docs-page__ask-btn"
            onClick={() => setAskOpen(true)}
            aria-label="Ask AI about the docs"
          >
            <Sparkles size={13} />
            <span>Ask AI</span>
          </button>
          <button
            type="button"
            className="docs-page__icon-btn"
            onClick={toggleTheme}
            aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
            title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
          >
            {isDark ? <Sun size={14} /> : <Moon size={14} />}
          </button>
        </div>
      </header>

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
          <div className="docs-page__placeholder-note" aria-hidden>
            Tip: values wrapped in angle brackets like <code>&lt;YOUR_API_KEY&gt;</code> are placeholders — replace them with your own values.
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
            <a href={SWAGGER_URL} target="_blank" rel="noreferrer">Swagger / OpenAPI</a>
            <a href={REDOC_URL} target="_blank" rel="noreferrer">ReDoc</a>
          </div>
        </aside>

        <section className="docs-page__content" aria-live="polite">
          {loadingDoc && !activeDoc && (
            <div className="docs-page__skeletons">
              <Skeleton variant="rect" height={40} />
              <Skeleton variant="rect" height={16} />
              <Skeleton variant="rect" height={16} />
              <Skeleton variant="rect" height={16} />
              <Skeleton variant="rect" height={160} />
            </div>
          )}
          {!loadingDoc && error && (
            <EmptyState
              title="Could not load this document"
              sub={error}
              action={<Button variant="secondary" size="sm" onClick={() => { if (selectedSlug) navigate(`/docs/${selectedSlug}`) }}>Retry</Button>}
            />
          )}
          {!loadingDoc && !error && activeDoc?.content && (
            <article className="docs-page__article">
              <MarkdownDoc content={activeDoc.content} className="docs-page__markdown" />
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
                      ? <ReactMarkdown remarkPlugins={[remarkGfm]} className="docs-ask__md">{msg.content}</ReactMarkdown>
                      : msg.content}
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
