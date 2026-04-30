// Aztea brand mark — layered yantra-style mandala.
// Three concentric rings, all 8-fold or 12-fold symmetric:
//   • outer ring with 12 diamond ticks (rotates clockwise, 50s)
//   • middle 8-petal lotus star (rotates counter-clockwise, 36s)
//   • inner ring + filled diamond bindu (gentle pulse)
// Uses SMIL <animateTransform> so the same SVG works in favicons and in DOM.
//
// Bilateral + 8-fold rotational symmetry. Color via currentColor.

const TICKS = 12
const PETALS = 8

function ring(radius, opacity = 0.45, dash = null) {
  return (
    <circle cx="0" cy="0" r={radius}
      stroke="currentColor" strokeWidth="0.9" fill="none"
      opacity={opacity} strokeDasharray={dash || undefined} />
  )
}

export default function AzteaMark({ size = 24, className = '', animate = true }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="-32 -32 64 64"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* ── Outer ring + 12 diamond ticks ── */}
      <g>
        {ring(28, 0.45)}
        <g>
          {Array.from({ length: TICKS }).map((_, i) => {
            const a = (i * 360 / TICKS)
            return (
              <g key={i} transform={`rotate(${a})`}>
                <rect x="-1.6" y="-30" width="3.2" height="3.2"
                  fill="currentColor" opacity="0.7"
                  transform="rotate(45 0 -28.4)" />
              </g>
            )
          })}
          {animate && (
            <animateTransform
              attributeName="transform"
              type="rotate"
              from="0"
              to="360"
              dur="50s"
              repeatCount="indefinite"
            />
          )}
        </g>
      </g>

      {/* ── Middle: 8-petal lotus / star ── */}
      <g>
        {Array.from({ length: PETALS }).map((_, i) => {
          const a = (i * 360 / PETALS)
          return (
            <path key={i}
              d="M 0 -22 Q 5 -12 0 -4 Q -5 -12 0 -22 Z"
              transform={`rotate(${a})`}
              stroke="currentColor"
              strokeWidth="1.2"
              fill="none"
              opacity="0.78" />
          )
        })}
        {animate && (
          <animateTransform
            attributeName="transform"
            type="rotate"
            from="360"
            to="0"
            dur="36s"
            repeatCount="indefinite"
          />
        )}
      </g>

      {/* ── Inner ring ── */}
      {ring(8, 0.55)}

      {/* ── Center bindu (filled diamond) ── */}
      <g>
        <rect x="-3.5" y="-3.5" width="7" height="7"
          fill="currentColor"
          transform="rotate(45)" />
        {animate && (
          <animateTransform
            attributeName="transform"
            type="scale"
            values="1;0.78;1"
            dur="4.5s"
            repeatCount="indefinite"
          />
        )}
      </g>
    </svg>
  )
}
