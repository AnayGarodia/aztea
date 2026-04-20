import { motion } from 'motion/react'
import { easeExpo } from '../../theme/motion'

const reduced =
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export default function ProgressRing({
  value,
  max = 100,
  size = 48,
  strokeWidth = 4,
  label,
  color = 'var(--accent)',
  className,
  style,
}) {
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius
  const pct = Math.min(Math.max((value ?? 0) / max, 0), 1)
  const offset = circumference * (1 - pct)

  return (
    <div
      className={className}
      style={{ position: 'relative', width: size, height: size, flexShrink: 0, ...style }}
    >
      <svg
        width={size}
        height={size}
        style={{ transform: 'rotate(-90deg)', display: 'block' }}
        aria-hidden
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--border)"
          strokeWidth={strokeWidth}
        />
        <motion.circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={circumference}
          initial={reduced ? { strokeDashoffset: offset } : { strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: offset }}
          transition={{ duration: 0.9, ease: easeExpo }}
        />
      </svg>
      {label != null && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '0.6875rem',
            fontWeight: 600,
            color: 'var(--text-secondary)',
            lineHeight: 1,
          }}
        >
          {label}
        </div>
      )}
    </div>
  )
}
