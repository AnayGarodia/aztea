// Aztea brand mark — 8-point star (rub el hizb / Indian sun chakra).
// Highly symmetric: 8-fold rotational + bilateral. Two overlapping squares
// rotated 45° to each other form the octagram, ringed by a hairline circle,
// pinned by a diamond center.
//
// Animation: outer square rotates clockwise (60s), inner square counter-
// clockwise (40s), center diamond pulses gently. Animations pause on
// prefers-reduced-motion. Color comes from currentColor.
import './AzteaMark.css'

export default function AzteaMark({ size = 24, className = '', animate = true }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      className={`aztea-mark ${animate ? 'aztea-mark--anim' : ''} ${className}`.trim()}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* Outer hairline ring */}
      <circle cx="24" cy="24" r="22" stroke="currentColor" strokeWidth="0.9" opacity="0.45" />

      {/* Octagram = two squares rotated 45° from each other */}
      <g className="aztea-mark__sq aztea-mark__sq--a">
        <rect
          x="8"
          y="8"
          width="32"
          height="32"
          stroke="currentColor"
          strokeWidth="1.6"
          fill="none"
        />
      </g>
      <g className="aztea-mark__sq aztea-mark__sq--b">
        <rect
          x="8"
          y="8"
          width="32"
          height="32"
          stroke="currentColor"
          strokeWidth="1.6"
          fill="none"
          transform="rotate(45 24 24)"
        />
      </g>

      {/* Inner small ring + diamond keystone */}
      <circle cx="24" cy="24" r="6" stroke="currentColor" strokeWidth="0.9" opacity="0.6" fill="none" />
      <g className="aztea-mark__diamond">
        <rect
          x="20.5"
          y="20.5"
          width="7"
          height="7"
          fill="currentColor"
          transform="rotate(45 24 24)"
        />
      </g>
    </svg>
  )
}
