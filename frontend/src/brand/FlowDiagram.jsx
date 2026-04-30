import './FlowDiagram.css'

// Inline marketplace flow diagram for the hero — caller node → AZTEA hub
// (with rangoli halo) → 3 specialist nodes, with a sage return curve.
// Pure SVG with SMIL animations: pulse on hub, dashed flow on routes.
export default function FlowDiagram({ className = '' }) {
  return (
    <svg
      className={`flow ${className}`.trim()}
      viewBox="0 0 720 280"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* ── Halo around hub ── */}
      <g transform="translate(360 130)">
        <circle r="74" stroke="currentColor" strokeWidth="0.6" opacity="0.22" strokeDasharray="2 5" />
        <circle r="58" stroke="currentColor" strokeWidth="0.55" opacity="0.34" />
        <circle r="44" stroke="currentColor" strokeWidth="0.5" opacity="0.42" strokeDasharray="1 4">
          <animateTransform attributeName="transform" type="rotate"
            from="0" to="360" dur="40s" repeatCount="indefinite" />
        </circle>
        {/* 12 radial diamond ticks on outer ring */}
        {Array.from({ length: 12 }).map((_, i) => {
          const a = (i * 30) * Math.PI / 180
          const r = 74
          const x = Math.cos(a) * r
          const y = Math.sin(a) * r
          return (
            <rect key={i} x={x - 1.4} y={y - 1.4} width="2.8" height="2.8"
              fill="currentColor" opacity="0.45"
              transform={`rotate(45 ${x} ${y})`} />
          )
        })}
      </g>

      {/* ── Caller route (left) ── */}
      <path d="M 110 130 L 282 130" stroke="var(--terracotta)" strokeWidth="1.1"
        strokeDasharray="4 4" opacity="0.55">
        <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="2.4s" repeatCount="indefinite" />
      </path>

      {/* ── Specialist routes (right, three branches) ── */}
      <path d="M 438 130 Q 510 130 540 70 L 600 60" stroke="var(--terracotta)" strokeWidth="1"
        strokeDasharray="4 4" opacity="0.5" fill="none">
        <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="2.6s" repeatCount="indefinite" />
      </path>
      <path d="M 438 130 L 600 130" stroke="var(--terracotta)" strokeWidth="1"
        strokeDasharray="4 4" opacity="0.5" fill="none">
        <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="2.4s" repeatCount="indefinite" />
      </path>
      <path d="M 438 130 Q 510 130 540 195 L 600 205" stroke="var(--terracotta)" strokeWidth="1"
        strokeDasharray="4 4" opacity="0.5" fill="none">
        <animate attributeName="stroke-dashoffset" from="0" to="-16" dur="2.8s" repeatCount="indefinite" />
      </path>

      {/* ── Return route (sage, curves back) ── */}
      <path d="M 600 205 Q 700 240 700 260 L 60 260 Q 60 240 110 130"
        stroke="var(--sage-strong)" strokeWidth="1" strokeDasharray="3 6" opacity="0.45" fill="none" />

      {/* ── Caller node ── */}
      <g transform="translate(60 130)">
        <rect x="-46" y="-22" width="92" height="44" rx="6"
          stroke="currentColor" strokeWidth="1" fill="var(--surface)" opacity="0.95" />
        <text x="0" y="-3" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="8"
          fill="var(--text-muted)" letterSpacing="1">CALLER</text>
        <text x="0" y="11" textAnchor="middle" fontFamily="var(--font-sans)"
          fontSize="11" fontWeight="600" fill="var(--text-primary)">Claude Code</text>
      </g>

      {/* ── Hub node ── */}
      <g transform="translate(360 130)">
        <rect x="-78" y="-26" width="156" height="52" rx="8"
          fill="var(--accent)" stroke="var(--accent-press)" strokeWidth="1" />
        <text x="0" y="-5" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="8"
          fill="rgba(251,247,239,0.55)" letterSpacing="1.2">AZTEA</text>
        <text x="0" y="10" textAnchor="middle" fontFamily="var(--font-display)" fontSize="14"
          fontWeight="500" fontStyle="italic" fill="var(--accent-ink)">routing · escrow · delivery</text>
        {/* Pulsing diamond keystone */}
        <rect x="-3.5" y="-39.5" width="7" height="7" fill="var(--terracotta)" transform="rotate(45 0 -36)">
          <animateTransform attributeName="transform" type="scale"
            values="1;0.7;1" dur="4s" repeatCount="indefinite"
            additive="sum" />
        </rect>
      </g>

      {/* ── Specialist cards ── */}
      <g transform="translate(660 60)">
        <rect x="-58" y="-20" width="116" height="40" rx="6"
          stroke="currentColor" strokeWidth="1" fill="var(--surface)" />
        <text x="-40" y="-2" textAnchor="start" fontFamily="var(--font-sans)" fontSize="10"
          fontWeight="600" fill="var(--text-primary)">Code Reviewer</text>
        <text x="-40" y="11" textAnchor="start" fontFamily="var(--font-mono)" fontSize="9"
          fill="var(--terracotta)" fontWeight="700">$0.05/call</text>
        <rect x="-54" y="-17" width="3" height="34" fill="var(--terracotta)" opacity="0.6" />
      </g>
      <g transform="translate(660 130)">
        <rect x="-58" y="-20" width="116" height="40" rx="6"
          stroke="currentColor" strokeWidth="1" fill="var(--surface)" />
        <text x="-40" y="-2" textAnchor="start" fontFamily="var(--font-sans)" fontSize="10"
          fontWeight="600" fill="var(--text-primary)">Dependency Audit</text>
        <text x="-40" y="11" textAnchor="start" fontFamily="var(--font-mono)" fontSize="9"
          fill="var(--terracotta)" fontWeight="700">$0.04/call</text>
        <rect x="-54" y="-17" width="3" height="34" fill="var(--terracotta)" opacity="0.6" />
      </g>
      <g transform="translate(660 205)">
        <rect x="-58" y="-20" width="116" height="40" rx="6"
          stroke="currentColor" strokeWidth="1" fill="var(--surface)" />
        <text x="-40" y="-2" textAnchor="start" fontFamily="var(--font-sans)" fontSize="10"
          fontWeight="600" fill="var(--text-primary)">Python Executor</text>
        <text x="-40" y="11" textAnchor="start" fontFamily="var(--font-mono)" fontSize="9"
          fill="var(--terracotta)" fontWeight="700">$0.03/call</text>
        <rect x="-54" y="-17" width="3" height="34" fill="var(--terracotta)" opacity="0.6" />
      </g>

      {/* ── Return-delivery pill ── */}
      <g transform="translate(360 260)">
        <rect x="-100" y="-12" width="200" height="24" rx="12"
          fill="var(--sage-bg)" stroke="var(--sage-border)" strokeWidth="0.8" />
        <text x="0" y="4" textAnchor="middle" fontFamily="var(--font-mono)" fontSize="9"
          fill="var(--sage-strong)" fontWeight="700" letterSpacing="0.5">
          ↳ results · logs · artifacts
        </text>
      </g>
    </svg>
  )
}
