// Aztea brand mark.
// Architectural mehrab (Indian pointed-arch portal) carved out of a teal
// square, with a small diamond keystone — reads as a marketplace doorway.
// Renders cleanly at any size; uses currentColor so it inherits theme color.
export default function AzteaMark({ size = 24, className = '', ink = 'var(--accent-ink)' }) {
  const id = `mehrab-${size}`
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <defs>
        <mask id={id}>
          <rect width="24" height="24" fill="white" />
          {/* Carve out the pointed mehrab arch */}
          <path
            d="M6 21 V13.5 C6 11 7.6 8 12 8 C16.4 8 18 11 18 13.5 V21 H15 V14 C15 12.4 13.9 11 12 11 C10.1 11 9 12.4 9 14 V21 Z"
            fill="black"
          />
          {/* Diamond keystone above arch */}
          <rect x="10.6" y="3.6" width="2.8" height="2.8" fill="black" transform="rotate(45 12 5)" />
        </mask>
      </defs>
      <rect
        x="1.5"
        y="1.5"
        width="21"
        height="21"
        rx="5"
        fill="currentColor"
        mask={`url(#${id})`}
      />
    </svg>
  )
}
