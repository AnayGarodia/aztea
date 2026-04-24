import { motion } from 'motion/react'
import './TrustGauge.css'

// SVG arc gauge - draws from 7 o'clock to 5 o'clock (270° arc matching design system)
function describeArc(cx, cy, r, startAngle, endAngle) {
  const toRad = (deg) => (deg * Math.PI) / 180
  const x1 = cx + r * Math.cos(toRad(startAngle))
  const y1 = cy + r * Math.sin(toRad(startAngle))
  const x2 = cx + r * Math.cos(toRad(endAngle))
  const y2 = cy + r * Math.sin(toRad(endAngle))
  const largeArc = endAngle - startAngle > 180 ? 1 : 0
  return `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2}`
}

const SIZE  = 140
const CX    = SIZE / 2   // 70
const CY    = SIZE / 2   // 70
const R     = 50
const START = 135
const TOTAL = 270

function TrustArc({ pct }) {
  const end = START + TOTAL * Math.max(0, Math.min(1, pct))
  const trackPath = describeArc(CX, CY, R, START, START + TOTAL)
  const fillPath  = pct > 0 ? describeArc(CX, CY, R, START, end) : null
  const color = pct >= 0.8 ? 'var(--positive)' : pct >= 0.5 ? 'var(--warn)' : 'var(--negative)'

  return (
    <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} aria-hidden="true">
      {/* Track */}
      <path d={trackPath} fill="none" stroke="var(--border)" strokeWidth="8" strokeLinecap="round" />
      {/* Fill */}
      {fillPath && (
        <motion.path
          d={fillPath}
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeLinecap="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 0.8, ease: [0.2, 0.8, 0.2, 1] }}
        />
      )}
      {/* Center score */}
      <text
        x={CX} y={CY - 2}
        textAnchor="middle"
        fontSize="22"
        fontWeight="500"
        fontFamily="'Fraunces', Georgia, serif"
        fill="var(--text-primary)"
        letterSpacing="-0.02em"
      >
        {pct != null ? Math.round(pct * 100) : '-'}
      </text>
      <text
        x={CX} y={CY + 16}
        textAnchor="middle"
        fontSize="8"
        fontFamily="'Plus Jakarta Sans', system-ui, sans-serif"
        fill="var(--text-muted)"
        fontWeight="700"
        letterSpacing="0.08em"
      >
        TRUST
      </text>
    </svg>
  )
}

function CallBar({ total, max, color = 'var(--accent)' }) {
  const pct = max > 0 ? total / max : 0
  return (
    <div className="tg__bar-wrap">
      <div className="tg__bar-track">
        <motion.div
          className="tg__bar-fill"
          style={{ background: color }}
          initial={{ width: 0 }}
          animate={{ width: `${Math.min(100, pct * 100)}%` }}
          transition={{ duration: 0.8, ease: [0.2, 0.8, 0.2, 1] }}
        />
      </div>
    </div>
  )
}

export default function TrustGauge({ agent }) {
  // trust_score is 0-100 from backend; normalize to 0-1 for arc
  const trustPct      = agent?.trust_score      != null ? agent.trust_score / 100 : null
  const confidencePct = agent?.confidence_score != null ? agent.confidence_score  : null
  const calls         = agent?.total_calls       ?? 0
  const ms            = agent?.avg_latency_ms    ?? null

  return (
    <div className="tg">
      {/* Arc gauge */}
      <div className="tg__gauge">
        <TrustArc pct={trustPct ?? 0} />
        <p className="tg__gauge-label">Trust score</p>
      </div>

      {/* Stats grid */}
      <div className="tg__stats">
        <div className="tg__stat">
          <span className="tg__stat-val">
            {calls > 0 ? calls.toLocaleString() : '-'}
          </span>
          <span className="tg__stat-key">Total jobs</span>
          {calls > 0 && <CallBar total={calls} max={Math.max(calls, 200)} color="var(--accent)" />}
        </div>

        <div className="tg__stat">
          <span className="tg__stat-val">
            {ms != null
              ? ms < 1000
                ? `${Math.round(ms)}ms`
                : `${(ms / 1000).toFixed(1)}s`
              : '-'}
          </span>
          <span className="tg__stat-key">Avg latency</span>
        </div>

        <div className="tg__stat">
          <span className="tg__stat-val">
            {confidencePct != null ? `${Math.round(confidencePct * 100)}%` : '-'}
          </span>
          <span className="tg__stat-key">Confidence</span>
          {confidencePct != null && (
            <CallBar total={confidencePct} max={1} color="var(--positive)" />
          )}
        </div>

        <div className="tg__stat">
          <span className="tg__stat-val">
            ${Number(agent?.price_per_call_usd ?? 0).toFixed(2)}
          </span>
          <span className="tg__stat-key">Per call</span>
        </div>
      </div>
    </div>
  )
}
