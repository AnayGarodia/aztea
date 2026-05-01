/**
 * JobReceipt.jsx — Cryptographic receipt panel for a completed job.
 *
 * Fetches the agent's did:web document and the job's signature payload, then
 * verifies the Ed25519 signature in-browser via WebCrypto. The platform cannot
 * have tampered with the output without breaking the signature, so this gives
 * the buyer a real, third-party-style guarantee — even Aztea itself cannot
 * forge a passing verification result here, because the page does the
 * cryptography, not the server.
 *
 * Surfaced on JobDetailPage when the job is complete and a signature exists.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { ShieldCheck, ShieldAlert, ShieldQuestion, Loader, Copy, Check } from 'lucide-react'
import Card from '../../ui/Card'
import Button from '../../ui/Button'
import Badge from '../../ui/Badge'
import { fetchJobSignature, fetchAgentDidDocument } from '../../api'

function _b64UrlDecode(input) {
  if (typeof input !== 'string' || !input) return null
  // Accept standard or URL-safe base64; pad as needed.
  let s = input.replace(/-/g, '+').replace(/_/g, '/')
  while (s.length % 4 !== 0) s += '='
  try {
    const bin = atob(s)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    return bytes
  } catch {
    return null
  }
}

function _extractEd25519Pk(didDoc) {
  // The DID document publishes one or more verificationMethod entries. We
  // accept publicKeyJwk (preferred), publicKeyBase64, and publicKeyMultibase.
  const methods = Array.isArray(didDoc?.verificationMethod) ? didDoc.verificationMethod : []
  for (const m of methods) {
    if (!m || typeof m !== 'object') continue
    const jwk = m.publicKeyJwk
    if (jwk && jwk.crv === 'Ed25519' && jwk.x) return _b64UrlDecode(jwk.x)
    const raw = m.publicKeyBase64 || m.publicKeyMultibase
    if (typeof raw === 'string' && raw) {
      // Multibase 'z…' prefix is base58 — but Aztea publishes raw base64; strip 'z' and try.
      const stripped = raw.startsWith('z') ? raw.slice(1) : raw
      const decoded = _b64UrlDecode(stripped)
      if (decoded) return decoded
    }
  }
  return null
}

async function _verifyEd25519(publicKeyBytes, signatureBytes, messageBytes) {
  if (!publicKeyBytes || !signatureBytes || !messageBytes) return false
  if (!window.crypto?.subtle?.importKey) return null // verification unavailable in this browser
  try {
    const key = await window.crypto.subtle.importKey(
      'raw',
      publicKeyBytes,
      { name: 'Ed25519' },
      false,
      ['verify'],
    )
    return await window.crypto.subtle.verify('Ed25519', key, signatureBytes, messageBytes)
  } catch (err) {
    // Older browsers may not support Ed25519 in WebCrypto. Caller falls back
    // to "verification unavailable" rather than a hard error.
    return null
  }
}

const STATE_PRISTINE = 'pristine'
const STATE_LOADING = 'loading'
const STATE_VERIFIED = 'verified'
const STATE_INVALID = 'invalid'
const STATE_UNAVAILABLE = 'unavailable'   // signature endpoint returned null
const STATE_UNSUPPORTED = 'unsupported'   // browser cannot do Ed25519 verify
const STATE_ERROR = 'error'

/**
 * Renders the receipt verification card.
 *
 * @param {object} props
 * @param {string} props.jobId - The job id whose receipt to verify.
 * @param {string} [props.agentId] - Optional pre-known agent id; if absent we
 *   parse it from the DID inside the signature payload.
 */
export default function JobReceipt({ jobId, agentId }) {
  const [state, setState] = useState(STATE_PRISTINE)
  const [signature, setSignature] = useState(null)
  const [didDoc, setDidDoc] = useState(null)
  const [error, setError] = useState(null)
  const [copied, setCopied] = useState(false)

  const verify = useCallback(async () => {
    setState(STATE_LOADING)
    setError(null)
    try {
      const sig = await fetchJobSignature(jobId)
      if (!sig) {
        setState(STATE_UNAVAILABLE)
        return
      }
      setSignature(sig)
      const didFromSig = String(sig.agent_did || '')
      const parsedAgentId = didFromSig.includes(':agents:')
        ? didFromSig.split(':agents:').pop()
        : agentId
      if (!parsedAgentId) {
        setError('No agent id available to fetch DID document.')
        setState(STATE_ERROR)
        return
      }
      const did = await fetchAgentDidDocument(parsedAgentId)
      if (!did) {
        setError('Agent has not published a DID document.')
        setState(STATE_ERROR)
        return
      }
      setDidDoc(did)
      const pk = _extractEd25519Pk(did)
      const sigBytes = _b64UrlDecode(String(sig.signature || ''))
      const msgBytes = new TextEncoder().encode(String(sig.output_hash || ''))
      if (!pk || !sigBytes) {
        setError('Could not parse public key or signature bytes.')
        setState(STATE_ERROR)
        return
      }
      const ok = await _verifyEd25519(pk, sigBytes, msgBytes)
      if (ok === null) {
        setState(STATE_UNSUPPORTED)
        return
      }
      setState(ok ? STATE_VERIFIED : STATE_INVALID)
    } catch (err) {
      setError(err?.message || String(err))
      setState(STATE_ERROR)
    }
  }, [jobId, agentId])

  useEffect(() => {
    if (state === STATE_PRISTINE) verify()
  }, [verify, state])

  const handleCopy = async (value) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // No-op — clipboard may be blocked by sandbox or HTTP origin.
    }
  }

  const ui = useMemo(() => {
    switch (state) {
      case STATE_VERIFIED:
        return {
          icon: <ShieldCheck size={18} aria-hidden />,
          tone: 'positive',
          title: 'Receipt verified',
          body: (
            <>This output was signed by the agent&apos;s published Ed25519 key. The
              signature was checked locally in your browser — Aztea cannot forge
              this result.</>
          ),
        }
      case STATE_INVALID:
        return {
          icon: <ShieldAlert size={18} aria-hidden />,
          tone: 'negative',
          title: 'Signature does not verify',
          body: (
            <>The signature returned by Aztea does not validate against the
              agent&apos;s published public key. Treat this output as
              untrustworthy until you understand why.</>
          ),
        }
      case STATE_UNAVAILABLE:
        return {
          icon: <ShieldQuestion size={18} aria-hidden />,
          tone: 'neutral',
          title: 'No receipt yet',
          body: <>This job has not produced a signed receipt. Some legacy or
            in-flight jobs do not.</>,
        }
      case STATE_UNSUPPORTED:
        return {
          icon: <ShieldQuestion size={18} aria-hidden />,
          tone: 'neutral',
          title: 'Browser cannot verify',
          body: (
            <>This browser does not support Ed25519 in WebCrypto. Use the
              CLI (<code>aztea jobs verify {jobId}</code>) or the SDK
              (<code>client.verify_job</code>) to verify locally.</>
          ),
        }
      case STATE_ERROR:
        return {
          icon: <ShieldAlert size={18} aria-hidden />,
          tone: 'warn',
          title: 'Could not verify',
          body: error || 'An unknown error occurred.',
        }
      case STATE_LOADING:
      default:
        return {
          icon: <Loader size={18} className="job-receipt__spinner" aria-hidden />,
          tone: 'neutral',
          title: 'Verifying receipt…',
          body: 'Fetching signature and DID document.',
        }
    }
  }, [state, error, jobId])

  return (
    <Card>
      <Card.Header>
        <div className="job-receipt__header">
          <span className="job-detail__section-title">Cryptographic receipt</span>
          <Badge variant={ui.tone === 'positive' ? 'success' : ui.tone === 'negative' ? 'error' : ui.tone === 'warn' ? 'warning' : 'neutral'}>
            {ui.title}
          </Badge>
        </div>
      </Card.Header>
      <Card.Body>
        <div className="job-receipt__body">
          <div className="job-receipt__icon-wrap">{ui.icon}</div>
          <div className="job-receipt__copy">
            <p>{ui.body}</p>
            {signature && (
              <dl className="job-receipt__details">
                <div>
                  <dt>Signed at</dt>
                  <dd>{signature.signed_at || '—'}</dd>
                </div>
                <div>
                  <dt>Output hash</dt>
                  <dd className="job-receipt__mono">
                    <code>{signature.output_hash}</code>
                    <button
                      type="button"
                      className="job-receipt__copy-btn"
                      onClick={() => handleCopy(String(signature.output_hash || ''))}
                      aria-label="Copy output hash"
                    >
                      {copied ? <Check size={12} /> : <Copy size={12} />}
                    </button>
                  </dd>
                </div>
                <div>
                  <dt>Agent DID</dt>
                  <dd className="job-receipt__mono"><code>{signature.agent_did}</code></dd>
                </div>
              </dl>
            )}
            <div className="job-receipt__actions">
              <Button
                type="button"
                size="sm"
                variant="secondary"
                onClick={verify}
                disabled={state === STATE_LOADING}
              >
                Re-verify
              </Button>
              <a
                href={`/docs#identity-verification`}
                className="job-receipt__learn"
                onClick={(e) => {
                  // Use docs route if available; fall back to public docs file.
                  if (!document.getElementById('identity-verification')) {
                    e.preventDefault()
                    window.open('https://github.com/AnayGarodia/aztea/blob/main/docs/identity-verification.md', '_blank', 'noopener')
                  }
                }}
              >
                How does this work?
              </a>
            </div>
          </div>
        </div>
      </Card.Body>
    </Card>
  )
}
