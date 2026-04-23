import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Moon, Sun, Menu, X, ArrowLeft } from 'lucide-react'
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

  const activeDocTitle = useMemo(() => {
    if (!selectedSlug) return ''
    const found = docs.find((d) => d.slug === selectedSlug)
    return found?.title ?? ''
  }, [docs, selectedSlug])

  const renderNavLinks = (onSelect) => (
    <nav className="docs-nav" aria-label="Documentation list">
      {docs.map((doc) => (
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
