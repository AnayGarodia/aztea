import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ChevronLeft } from 'lucide-react'
import Topbar from '../layout/Topbar'
import MarkdownDoc from '../ui/MarkdownDoc'
import { fetchPublicDoc } from '../api'
import { useAuth } from '../context/AuthContext'
import './LegalPage.css'

export default function LegalDocumentPage({ title, crumb, slug }) {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [content, setContent] = useState('')
  const [error, setError] = useState('')

  const handleBack = () => {
    if (window.history.length > 1) navigate(-1)
    else navigate('/')
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    fetchPublicDoc(slug)
      .then((doc) => {
        if (cancelled) return
        setContent(String(doc?.content ?? ''))
      })
      .catch((err) => {
        if (cancelled) return
        setError(err?.message || 'Failed to load this legal document.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [slug])

  const needsLegalAcceptance = Boolean(user?.legal_acceptance_required)

  return (
    <main className="legal-page">
      <Topbar crumbs={[{ label: crumb }]} />
      <div className="legal-page__scroll">
        <div className="legal-page__content">
          <button type="button" className="legal-page__back" onClick={handleBack}>
            <ChevronLeft size={14} />
            Back
          </button>
          {needsLegalAcceptance && (
            <div className="legal-page__accept-cta">
              <p>You must accept the latest Terms of Service and Privacy Policy before accessing the marketplace.</p>
              <Link to="/legal/accept" className="legal-page__accept-btn">
                Review and accept now
              </Link>
            </div>
          )}

          {loading && <p className="legal-page__status">Loading…</p>}
          {!loading && error && <p className="legal-page__status legal-page__status--error">{error}</p>}
          {!loading && !error && <MarkdownDoc content={content} className="legal-page__markdown" />}
        </div>
      </div>
    </main>
  )
}
