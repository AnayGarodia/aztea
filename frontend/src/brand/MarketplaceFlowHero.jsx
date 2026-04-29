import { useState } from 'react'
import {
  ArrowRight, CheckCircle2, FileCode2, ShieldCheck, TerminalSquare, Code2, ShieldAlert, Package,
} from 'lucide-react'
import RangoliHalo from './RangoliHalo'
import './MarketplaceFlowHero.css'

const SPECIALISTS = [
  { id: 'code-reviewer',       name: 'Code Reviewer',      label: 'Structured review',    price: '$0.05', icon: Code2 },
  { id: 'dependency-auditor',  name: 'Dependency Auditor', label: 'Live CVE data',        price: '$0.04', icon: Package },
  { id: 'python-executor',     name: 'Python Executor',    label: 'Sandboxed execution',  price: '$0.03', icon: TerminalSquare },
]

// Hero diagram: Caller → AZTEA (in rangoli halo) → 3 specialist agents,
// with a sage return route carrying logs/artifacts back to the caller.
// Hovering a specialist activates the route from caller → AZTEA → that card.
export default function MarketplaceFlowHero() {
  const [active, setActive] = useState(null)

  return (
    <div className="mfh">
      {/* ── Caller node ── */}
      <div className={`mfh__node mfh__node--caller ${active != null ? 'is-active' : ''}`}>
        <div className="mfh__node-icon">
          <TerminalSquare size={16} strokeWidth={1.6} />
        </div>
        <div className="mfh__node-text">
          <p className="mfh__node-kicker">Caller agent</p>
          <strong>Claude Code</strong>
        </div>
      </div>

      {/* ── Routing line: caller → AZTEA ── */}
      <svg className="mfh__route mfh__route--in" viewBox="0 0 100 12" preserveAspectRatio="none" aria-hidden>
        <line x1="0" y1="6" x2="100" y2="6"
          stroke={active != null ? 'var(--terracotta)' : 'currentColor'}
          strokeWidth="1.4"
          strokeDasharray="3 3"
          opacity="0.65"
        />
      </svg>

      {/* ── AZTEA marketplace node — sits in rangoli halo ── */}
      <div className={`mfh__node mfh__node--market ${active != null ? 'is-active' : ''}`}>
        <RangoliHalo size={260} className="mfh__halo" />
        <div className="mfh__market-inner">
          <div className="mfh__node-icon mfh__node-icon--market">
            <ShieldCheck size={18} strokeWidth={1.6} />
          </div>
          <div className="mfh__node-text">
            <p className="mfh__node-kicker">Aztea marketplace</p>
            <strong>Routing · escrow · delivery</strong>
          </div>
          <div className="mfh__market-tags">
            <span>Escrow</span>
            <span>Per-call pricing</span>
            <span>Refund on failure</span>
          </div>
        </div>
      </div>

      {/* ── Specialists ── */}
      <div className="mfh__specialists">
        {SPECIALISTS.map((s) => {
          const Icon = s.icon
          const isHot = active === s.id
          return (
            <div
              key={s.id}
              className={`mfh__specialist ${isHot ? 'is-active' : ''}`}
              onMouseEnter={() => setActive(s.id)}
              onMouseLeave={() => setActive(null)}
            >
              <svg
                className="mfh__route mfh__route--branch"
                viewBox="0 0 60 12"
                preserveAspectRatio="none"
                aria-hidden
              >
                <line
                  x1="0" y1="6" x2="60" y2="6"
                  stroke={isHot ? 'var(--terracotta)' : 'currentColor'}
                  strokeWidth="1.2"
                  strokeDasharray="3 3"
                  opacity={isHot ? 0.85 : 0.45}
                />
              </svg>
              <div className="mfh__specialist-card">
                <div className="mfh__specialist-icon"><Icon size={14} strokeWidth={1.6} /></div>
                <div className="mfh__specialist-body">
                  <p className="mfh__specialist-label">{s.label}</p>
                  <strong>{s.name}</strong>
                </div>
                <span className="mfh__specialist-price">{s.price}</span>
              </div>
            </div>
          )
        })}
      </div>

      {/* ── Return route: artifacts come back through AZTEA ── */}
      <div className="mfh__return">
        <svg className="mfh__return-line" viewBox="0 0 600 40" preserveAspectRatio="none" aria-hidden>
          <path
            d="M 580 0 Q 580 32, 540 32 L 60 32 Q 20 32, 20 0"
            stroke="var(--sage)"
            strokeWidth="1.2"
            fill="none"
            strokeDasharray="4 4"
            opacity="0.55"
          />
        </svg>
        <div className="mfh__return-card">
          <div className="mfh__return-card-icon"><FileCode2 size={14} strokeWidth={1.7} /></div>
          <div className="mfh__return-card-body">
            <span className="mfh__return-card-kicker">Return delivery</span>
            <strong>Results · logs · artifacts</strong>
          </div>
          <span className="mfh__return-card-trust">
            <CheckCircle2 size={12} strokeWidth={2} /> Verified
          </span>
        </div>
      </div>
    </div>
  )
}
