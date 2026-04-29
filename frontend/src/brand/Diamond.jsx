// Tiny ornamental diamond used under section labels and in nav active state.
// Keeps the Indian-modern visual rhythm without overdecorating.
export default function Diamond({ size = 5, color = 'currentColor', className = '', filled = true }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 10 10"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {filled ? (
        <path d="M5 0L10 5L5 10L0 5L5 0Z" fill={color} />
      ) : (
        <path d="M5 1L9 5L5 9L1 5L5 1Z" stroke={color} strokeWidth="1" fill="none" />
      )}
    </svg>
  )
}
