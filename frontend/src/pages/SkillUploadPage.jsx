import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import Topbar from '../layout/Topbar'
import Card from '../ui/Card'
import Button from '../ui/Button'
import Reveal from '../ui/motion/Reveal'
import { useAuth } from '../context/AuthContext'
import { validateSkillMd, createSkill, fetchAgents } from '../api'
import {
  CheckCircle, AlertTriangle, Upload, ArrowRight, ArrowLeft,
  Zap, TrendingUp, ExternalLink, FileText, Coins
} from 'lucide-react'
import './SkillUploadPage.css'

const STEPS = ['upload', 'price', 'confirm', 'live']

function StepBar({ step }) {
  const labels = ['Upload', 'Pricing', 'Confirm', 'Live']
  const idx = STEPS.indexOf(step)
  return (
    <div className="sup__stepbar">
      {labels.map((label, i) => (
        <div key={label} className={`sup__step ${i === idx ? 'sup__step--active' : ''} ${i < idx ? 'sup__step--done' : ''}`}>
          <div className="sup__step-dot">
            {i < idx ? <CheckCircle size={12} /> : <span>{i + 1}</span>}
          </div>
          <span className="sup__step-label">{label}</span>
          {i < labels.length - 1 && <div className="sup__step-line" />}
        </div>
      ))}
    </div>
  )
}

function EarningsCalculator({ price, callsPerMonth }) {
  const monthly = price * callsPerMonth * 0.9
  const scenarios = [
    { label: '100 calls/mo', calls: 100 },
    { label: '1k calls/mo',  calls: 1000 },
    { label: '10k calls/mo', calls: 10000 },
  ]
  const maxEarnings = price * 10000 * 0.9
  return (
    <div className="sup__calc">
      <p className="sup__calc-headline">
        At <strong>${price.toFixed(2)}/call</strong> you keep <strong>${monthly.toFixed(0)}/month</strong> at {callsPerMonth.toLocaleString()} calls.
      </p>
      <div className="sup__calc-bars">
        {scenarios.map(({ label, calls }) => {
          const earn = price * calls * 0.9
          const pct = maxEarnings > 0 ? Math.min((earn / maxEarnings) * 100, 100) : 0
          return (
            <div key={label} className="sup__calc-row">
              <span className="sup__calc-label">{label}</span>
              <div className="sup__calc-bar-wrap">
                <div className="sup__calc-bar" style={{ width: `${pct}%` }} />
              </div>
              <span className="sup__calc-earn">${earn >= 1000 ? `${(earn / 1000).toFixed(1)}k` : earn.toFixed(0)}</span>
            </div>
          )
        })}
      </div>
      <p className="sup__calc-note">Platform keeps 10%. No charge on failed calls.</p>
    </div>
  )
}

function ComparableAgents({ agents }) {
  if (!agents.length) return null
  return (
    <div className="sup__comps">
      <p className="sup__comps-label">Comparable agents on the marketplace</p>
      <div className="sup__comps-list">
        {agents.slice(0, 4).map(a => (
          <div key={a.agent_id} className="sup__comp-row">
            <span className="sup__comp-name">{a.name}</span>
            <span className="sup__comp-price">${Number(a.price_per_call_usd ?? 0).toFixed(2)}/call</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export default function SkillUploadPage() {
  const { apiKey } = useAuth()
  const navigate = useNavigate()

  const [step, setStep] = useState('upload')
  const [skillMd, setSkillMd] = useState('')
  const [validating, setValidating] = useState(false)
  const [preview, setPreview] = useState(null)
  const [uploadError, setUploadError] = useState('')
  const [price, setPrice] = useState(0.05)
  const [callsPreview, setCallsPreview] = useState(1000)
  const [agents, setAgents] = useState([])
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [result, setResult] = useState(null)
  const fileRef = useRef(null)
  const errorRef = useRef(null)

  useEffect(() => {
    fetchAgents(null).then(r => setAgents(r?.agents ?? [])).catch(() => {})
  }, [])

  const handleFile = (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => setSkillMd(ev.target.result ?? '')
    reader.readAsText(file)
  }

  const handleDrop = (e) => {
    e.preventDefault()
    const file = e.dataTransfer.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = ev => setSkillMd(ev.target.result ?? '')
    reader.readAsText(file)
  }

  const handleValidate = async () => {
    if (!skillMd.trim()) { setUploadError('Paste your SKILL.md content above.'); return }
    setUploadError('')
    setValidating(true)
    try {
      const data = await validateSkillMd(apiKey, skillMd)
      setPreview(data)
      setStep('price')
    } catch (e) {
      setUploadError(e?.message ?? 'Validation failed.')
      setTimeout(() => errorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50)
    } finally {
      setValidating(false)
    }
  }

  const handleList = async () => {
    setSubmitError('')
    setSubmitting(true)
    try {
      const data = await createSkill(apiKey, skillMd, price)
      setResult(data)
      setStep('live')
    } catch (e) {
      setSubmitError(e?.message ?? 'Failed to list skill.')
      setTimeout(() => errorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50)
    } finally {
      setSubmitting(false)
    }
  }

  const resetWizard = () => {
    setStep('upload')
    setSkillMd('')
    setPreview(null)
    setUploadError('')
    setSubmitError('')
    setResult(null)
    setPrice(0.05)
  }

  return (
    <main className="sup">
      <Topbar crumbs={[{ label: 'List a Skill' }]} />
      <div className="sup__scroll">
        <div className="sup__content">

          <Reveal>
            <div className="sup__header">
              <div className="sup__header-icon">
                <FileText size={22} />
              </div>
              <div>
                <h1 className="sup__title">List a skill</h1>
                <p className="sup__sub">Paste your SKILL.md, set a price, and start earning. Aztea handles execution and billing.</p>
              </div>
            </div>
            <StepBar step={step} />
          </Reveal>

          {/* ── Step 1: Upload ── */}
          {step === 'upload' && (
            <Reveal delay={0.05}>
              <Card>
                <Card.Header>
                  <span className="sup__section-title">Paste your SKILL.md</span>
                </Card.Header>
                <Card.Body>
                  <div
                    className="sup__drop-zone"
                    onDragOver={e => e.preventDefault()}
                    onDrop={handleDrop}
                  >
                    <textarea
                      className="sup__md-input"
                      placeholder={`# My Skill\n\nDescription: What this skill does.\n\n---\n\nSystem prompt goes here...`}
                      value={skillMd}
                      onChange={e => setSkillMd(e.target.value)}
                      spellCheck={false}
                      rows={14}
                    />
                    <div className="sup__drop-hint">
                      or{' '}
                      <button type="button" className="sup__file-link" onClick={() => fileRef.current?.click()}>
                        upload a file
                      </button>
                      <input ref={fileRef} type="file" accept=".md,.txt" style={{ display: 'none' }} onChange={handleFile} />
                    </div>
                  </div>
                  {uploadError && <p className="sup__error" ref={errorRef}>{uploadError}</p>}
                </Card.Body>
                <Card.Footer>
                  <Button variant="primary" loading={validating} onClick={handleValidate} disabled={!skillMd.trim()}>
                    Preview &amp; continue
                    <ArrowRight size={14} />
                  </Button>
                </Card.Footer>
              </Card>

              <div className="sup__format-hint">
                <p className="sup__format-title">SKILL.md format</p>
                <pre className="sup__format-code">{`# Skill Name\n\nDescription: One-line description.\nEmoji: 🔍\nPrice: $0.05\n\n---\n\nYour system prompt here. This is what Aztea\nuses to execute the skill on every call.`}</pre>
              </div>
            </Reveal>
          )}

          {/* ── Step 2: Pricing ── */}
          {step === 'price' && preview && (
            <Reveal delay={0.05}>
              {/* Parsed preview */}
              <Card>
                <Card.Header>
                  <span className="sup__section-title">Parsed skill</span>
                </Card.Header>
                <Card.Body>
                  <div className="sup__preview">
                    {preview.registration_preview?.emoji && (
                      <span className="sup__preview-emoji">{preview.registration_preview.emoji}</span>
                    )}
                    <div>
                      <p className="sup__preview-name">{preview.name}</p>
                      <p className="sup__preview-desc">{preview.description}</p>
                    </div>
                  </div>
                  {preview.warnings?.length > 0 && (
                    <div className="sup__warnings">
                      {preview.warnings.map((w, i) => (
                        <div key={i} className="sup__warning">
                          <AlertTriangle size={13} />
                          <span>{w}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </Card.Body>
              </Card>

              {/* Price setter */}
              <Card style={{ marginTop: 16 }}>
                <Card.Header>
                  <span className="sup__section-title">Set your price</span>
                </Card.Header>
                <Card.Body>
                  <div className="sup__price-row">
                    <div className="sup__price-input-wrap">
                      <span className="sup__price-prefix">$</span>
                      <input
                        type="number"
                        className="sup__price-input"
                        value={price}
                        min={0.00}
                        max={25}
                        step={0.01}
                        onChange={e => setPrice(Math.max(0, Math.min(25, parseFloat(e.target.value) || 0)))}
                      />
                      <span className="sup__price-suffix">per call</span>
                    </div>
                    <span className="sup__price-take">You keep <strong>${(price * 0.9).toFixed(3)}</strong></span>
                  </div>
                  <input
                    type="range"
                    className="sup__slider"
                    min={0.01}
                    max={5}
                    step={0.01}
                    value={Math.min(price, 5)}
                    onChange={e => setPrice(parseFloat(e.target.value))}
                  />
                  <div className="sup__slider-labels">
                    <span>$0.01</span>
                    <span>$5.00</span>
                  </div>

                  <div className="sup__divider" />

                  <p className="sup__calls-label">
                    Estimate at{' '}
                    <select
                      className="sup__calls-select"
                      value={callsPreview}
                      onChange={e => setCallsPreview(Number(e.target.value))}
                    >
                      <option value={100}>100</option>
                      <option value={500}>500</option>
                      <option value={1000}>1,000</option>
                      <option value={5000}>5,000</option>
                      <option value={10000}>10,000</option>
                    </select>
                    {' '}calls/month
                  </p>
                  <EarningsCalculator price={price} callsPerMonth={callsPreview} />
                  <ComparableAgents agents={agents} />
                </Card.Body>
                <Card.Footer style={{ justifyContent: 'space-between' }}>
                  <Button variant="ghost" onClick={() => setStep('upload')}>
                    <ArrowLeft size={14} /> Back
                  </Button>
                  <Button variant="primary" onClick={() => setStep('confirm')}>
                    Continue <ArrowRight size={14} />
                  </Button>
                </Card.Footer>
              </Card>
            </Reveal>
          )}

          {/* ── Step 3: Confirm ── */}
          {step === 'confirm' && preview && (
            <Reveal delay={0.05}>
              <Card>
                <Card.Header>
                  <span className="sup__section-title">Review and list</span>
                </Card.Header>
                <Card.Body>
                  <div className="sup__confirm-rows">
                    <div className="sup__confirm-row">
                      <span className="sup__confirm-key">Skill name</span>
                      <span className="sup__confirm-val">{preview.name}</span>
                    </div>
                    <div className="sup__confirm-row">
                      <span className="sup__confirm-key">Description</span>
                      <span className="sup__confirm-val">{preview.description}</span>
                    </div>
                    <div className="sup__confirm-row">
                      <span className="sup__confirm-key">Price per call</span>
                      <span className="sup__confirm-val t-mono">${price.toFixed(2)}</span>
                    </div>
                    <div className="sup__confirm-row">
                      <span className="sup__confirm-key">Your cut (90%)</span>
                      <span className="sup__confirm-val t-mono sup__confirm-val--earn">${(price * 0.9).toFixed(3)}</span>
                    </div>
                    <div className="sup__confirm-row">
                      <span className="sup__confirm-key">Execution</span>
                      <span className="sup__confirm-val">Aztea-hosted (skill://…)</span>
                    </div>
                    <div className="sup__confirm-row">
                      <span className="sup__confirm-key">Review</span>
                      <span className="sup__confirm-val sup__confirm-val--live">
                        <span className="sup__live-dot" /> Auto-approved — live immediately
                      </span>
                    </div>
                  </div>

                  <div className="sup__confirm-earnings">
                    <Coins size={15} />
                    <span>At 1,000 calls/month you'd earn <strong>${(price * 1000 * 0.9).toFixed(0)}</strong>. At 10,000 you'd earn <strong>${(price * 10000 * 0.9).toFixed(0)}</strong>.</span>
                  </div>

                  {submitError && <p className="sup__error" ref={errorRef} style={{ marginTop: 16 }}>{submitError}</p>}
                </Card.Body>
                <Card.Footer style={{ justifyContent: 'space-between' }}>
                  <Button variant="ghost" onClick={() => setStep('price')}>
                    <ArrowLeft size={14} /> Back
                  </Button>
                  <Button variant="primary" loading={submitting} onClick={handleList}>
                    List my skill <Zap size={14} />
                  </Button>
                </Card.Footer>
              </Card>
            </Reveal>
          )}

          {/* ── Step 4: Live ── */}
          {step === 'live' && result && (
            <Reveal delay={0.05}>
              <div className="sup__success">
                <div className="sup__success-icon">
                  <CheckCircle size={36} />
                </div>
                <h2 className="sup__success-title">Your skill is live</h2>
                <p className="sup__success-name">{result.name}</p>
                <p className="sup__success-sub">
                  Callers can discover and hire it right now. You'll be paid automatically after each successful job.
                </p>

                <div className="sup__success-meta">
                  <div className="sup__success-row">
                    <span className="sup__success-key">Endpoint</span>
                    <code className="sup__success-val-mono">{result.endpoint_url}</code>
                  </div>
                  <div className="sup__success-row">
                    <span className="sup__success-key">Price</span>
                    <code className="sup__success-val-mono">${Number(result.price_per_call_usd).toFixed(2)}/call</code>
                  </div>
                  <div className="sup__success-row">
                    <span className="sup__success-key">Your cut</span>
                    <code className="sup__success-val-mono sup__success-val--earn">${(Number(result.price_per_call_usd) * 0.9).toFixed(3)}/call</code>
                  </div>
                </div>

                <div className="sup__success-actions">
                  <Link to="/agents">
                    <Button variant="primary">
                      View in marketplace <ExternalLink size={13} />
                    </Button>
                  </Link>
                  <Link to="/worker">
                    <Button variant="secondary">Worker dashboard</Button>
                  </Link>
                  <Button variant="ghost" onClick={resetWizard}>List another</Button>
                </div>
              </div>
            </Reveal>
          )}

        </div>
      </div>
    </main>
  )
}
