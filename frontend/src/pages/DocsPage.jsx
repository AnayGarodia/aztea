import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import { fetchPublicDoc, fetchPublicDocsIndex } from '../api'
import MarkdownDoc from '../ui/MarkdownDoc'
import './DocsPage.css'

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

  useEffect(() => {
    let cancelled = false
    setLoadingList(true)
    setError('')
    fetchPublicDocsIndex()
      .then((data) => {
        if (cancelled) return
        const nextDocs = Array.isArray(data?.docs) ? data.docs : []
        setDocs(nextDocs)
        if (!nextDocs.length) {
          setActiveDoc(null)
          setError('Documentation is currently unavailable.')
        }
      })
      .catch((err) => {
        if (cancelled) return
        setError(err?.message || 'Failed to load docs.')
      })
      .finally(() => {
        if (!cancelled) setLoadingList(false)
      })
    return () => { cancelled = true }
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

  return (
    <main className="docs-page">
      <Topbar crumbs={[{ label: 'Documentation' }]} />
      <div className="docs-page__layout">
        <aside className="docs-page__sidebar">
          <Link to="/" className="docs-page__home-link">← Home</Link>
          <h1 className="docs-page__title">Docs</h1>
          <p className="docs-page__subtitle">Platform documentation.</p>
          <nav className="docs-page__nav" aria-label="Documentation list">
            {docs.map((doc) => (
              <Link
                key={doc.slug}
                to={`/docs/${doc.slug}`}
                className={`docs-page__nav-link${doc.slug === selectedSlug ? ' docs-page__nav-link--active' : ''}`}
              >
                {doc.title}
              </Link>
            ))}
          </nav>
          <div className="docs-page__api-links">
            <a href={SWAGGER_URL} target="_blank" rel="noreferrer">Swagger API docs</a>
            <a href={REDOC_URL} target="_blank" rel="noreferrer">ReDoc API docs</a>
          </div>
        </aside>

        <section className="docs-page__content" aria-live="polite">
          {(loadingList || loadingDoc) && <p className="docs-page__status">Loading…</p>}
          {!loadingList && error && <p className="docs-page__status docs-page__status--error">{error}</p>}
          {!loadingDoc && !error && activeDoc?.content && (
            <MarkdownDoc content={activeDoc.content} className="docs-page__markdown" />
          )}
        </section>
      </div>
    </main>
  )
}
