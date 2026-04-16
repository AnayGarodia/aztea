import { motion } from 'motion/react'
import './TrustGauge.css'

// SVG arc gauge — draws from 7 o'clock to 5 o'clock (220° arc)
function describeArc(cx, cy, r, startAngle, endAngle) {
  const toRad = (deg) => (deg * Math.PI) / 180
  const x1 = cx + r * Math.cos(toRad(startAngle))
  const y1 = cy + r * Math.sin(toRad(startAngle))
  const x2 = cx + r * Math.cos(toRad(endAngle))
  const y2 = cy + r * Math.sin(toRad(endAngle))
  const largeArc = endAngle - startAngle > 180 ? 1 : 0
  return `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2}`
}

const SIZE     = 120
const CX       = SIZE / 2
const CY       = SIZE / 2
const R        = 42
const START    = 135   // degrees (7 o'clock)
const TOTAL    = 270   // arc sweeps 270°

function TrustArc({ pct }) {
  const end = START + TOTAL * Math.max(0, Math.min(1, pct))
  const trackPath  = describeArc(CX, CY, R, START, START + TOTAL)
  const fillPath   = pct > 0 ? describeArc(CX, CY, R, START, end) : null
  const strokeLen  = 2 * Math.PI * R
  const color = pct >= 0.8 ? 'var(--positive)' : pct >= 0.5 ? 'var(--warn)' : 'var(--negative)'

  return (
    <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} aria-hidden="true">
      {/* Track */}
      <path d={trackPath} fill="none" stroke="var(--border)" strokeWidth="6" strokeLinecap="round" />
      {/* Fill */}
      {fillPath && (
        <motion.path
          d={fillPath}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 0.8, ease: [0.2, 0.8, 0.2, 1] }}
        />
      )}
      {/* Center text */}
      <text x={CX} y={CY - 4} textAnchor="middle" fontSize="16" fontWeight="600" fontFamily="'Geist Mono', monospace" fill="var(--text-primary)" letterSpacing="-0.03em">
        {pct != null ? `${Math.round(pct * 100)}%` : '—'}
      </text>
      <text x={CX} y={CY + 13} textAnchor="middle" fontSize="7.5" fontFamily="'Geist', sans-serif" fill="var(--text-muted)" fontWeight="600" letterSpacing="0.05em" textTransform="uppercase">
        SUCCESS
      </text>
    </svg>
  )
}

function CallBar({ total, max }) {
  const pct = max > 0 ? total / max : 0
  return (
    <div className="tg__bar-wrap">
      <div className="tg__bar-track">
        <motion.div
          className="tg__bar-fill"
          initial={{ width: 0 }}
          animate={{ width: `${Math.min(100, pct * 100)}%` }}
          transition={{ duration: 0.8, ease: [0.2, 0.8, 0.2, 1] }}
        />
      </div>
    </div>
  )
}

export default function TrustGauge({ agent }) {
  const rate  = agent?.success_rate   ?? null
  const calls = agent?.total_calls    ?? 0
  const ms    = agent?.avg_latency_ms ?? null

  // Latency score: <1s = great (1.0), 3s = ok (0.5), >10s = poor (0.0)
  const latencyScore = ms != null ? Math.max(0, 1 - ms / 10000) : null

  return (
    <div className="tg">
      {/* Arc gauge — success rate */}
      <div className="tg__gauge">
        <TrustArc pct={rate} />
        <p className="tg__gauge-label">Trust score</p>
      </div>

      {/* Supplementary stats */}
      <div className="tg__stats">
        <div className="tg__stat">
          <span className="tg__stat-val">
            {calls > 0 ? calls.toLocaleString() : '—'}
          </span>
          <span className="tg__stat-key">Total calls</span>
          {calls > 0 && <CallBar total={calls} max={Math.max(calls, 200)} />}
        </div>

        <div className="tg__stat">
          <span className="tg__stat-val">
            {ms != null ? (ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`) : '—'}
          </span>
          <span className="tg__stat-key">Avg latency</span>
          {latencyScore != null && <CallBar total={latencyScore} max={1} />}
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
