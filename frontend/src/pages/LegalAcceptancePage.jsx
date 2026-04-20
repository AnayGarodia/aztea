import { useMemo, useState } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { authAcceptLegal } from '../api'
import { useAuth } from '../context/AuthContext'
import Button from '../ui/Button'
import './LegalAcceptancePage.css'

export default function LegalAcceptancePage() {
  const navigate = useNavigate()
  const { apiKey, user, refreshProfile } = useAuth()
  const [termsChecked, setTermsChecked] = useState(false)
  const [privacyChecked, setPrivacyChecked] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const needsAcceptance = Boolean(user?.legal_acceptance_required)
  const canSubmit = termsChecked && privacyChecked && !submitting
  const currentTermsVersion = useMemo(
    () => String(user?.terms_version_current ?? ''),
    [user?.terms_version_current]
  )
  const currentPrivacyVersion = useMemo(
    () => String(user?.privacy_version_current ?? ''),
    [user?.privacy_version_current]
  )

  if (!apiKey) {
    return <Navigate to="/welcome" replace />
  }
  if (!needsAcceptance) {
    return <Navigate to="/overview" replace />
  }

  const onAccept = async () => {
    setError('')
    if (!canSubmit) return
    setSubmitting(true)
    try {
      await authAcceptLegal(apiKey, currentTermsVersion, currentPrivacyVersion)
      await refreshProfile?.()
      navigate('/overview', { replace: true })
    } catch (err) {
      setError(err?.message || 'Failed to record acceptance. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="legal-accept">
      <div className="legal-accept__card">
        <p className="legal-accept__eyebrow">Required before access</p>
        <h1 className="legal-accept__title">Accept Terms and Privacy Policy</h1>
        <p className="legal-accept__desc">
          To use the marketplace, you must accept the current legal documents.
        </p>

        <div className="legal-accept__docs">
          <Link to="/terms" className="legal-accept__doc-link">Read Terms of Service</Link>
          <Link to="/privacy" className="legal-accept__doc-link">Read Privacy Policy</Link>
        </div>

        <label className="legal-accept__check">
          <input
            type="checkbox"
            checked={termsChecked}
            onChange={(e) => setTermsChecked(e.target.checked)}
          />
          <span>I have reviewed and agree to the Terms of Service ({currentTermsVersion}).</span>
        </label>

        <label className="legal-accept__check">
          <input
            type="checkbox"
            checked={privacyChecked}
            onChange={(e) => setPrivacyChecked(e.target.checked)}
          />
          <span>I have reviewed and acknowledge the Privacy Policy ({currentPrivacyVersion}).</span>
        </label>

        {error && <p className="legal-accept__error">{error}</p>}

        <Button
          type="button"
          variant="primary"
          size="md"
          loading={submitting}
          disabled={!canSubmit}
          className="legal-accept__btn"
          onClick={onAccept}
        >
          Accept and continue
        </Button>
      </div>
    </main>
  )
}
