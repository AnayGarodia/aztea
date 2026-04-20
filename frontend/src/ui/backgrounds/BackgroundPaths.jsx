import { useMemo } from 'react'
import { motion } from 'motion/react'
import './BackgroundPaths.css'

const VIEW_W = 1200
const VIEW_H = 760
const EDGE_PAD = 44
const START_X = -220
const END_X = 1420

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value))
}

function buildPaths(position, count) {
  const safeCount = Math.max(1, count)
  const usableHeight = VIEW_H - EDGE_PAD * 2
  const yStep = usableHeight / (safeCount + 1)

  return Array.from({ length: count }, (_, i) => {
    const yBase = EDGE_PAD + yStep * (i + 1)
    const wave = (i % 6) * (9 + yStep * 0.26)
    const bend = 72 + i * 5
    const y1 = clamp(yBase + wave * position * 0.52, EDGE_PAD, VIEW_H - EDGE_PAD)
    const y2 = clamp(yBase - bend * position * 0.22, EDGE_PAD, VIEW_H - EDGE_PAD)
    const y3 = clamp(yBase - wave * 0.46, EDGE_PAD, VIEW_H - EDGE_PAD)
    const opacity = 0.08 + (i / safeCount) * 0.34

    return {
      id: `${position}-${i}`,
      d: `M${START_X} ${yBase}C160 ${y1} 430 ${y2} 760 ${y3}C980 ${y3 + 24} 1120 ${y2} ${END_X} ${yBase + 8}`,
      width: 0.62 + i * 0.038,
      opacity,
      duration: 18 + (i % 9) * 1.4,
    }
  })
}

function FloatingPaths({ paths }) {
  return (
    <svg className="background-paths__svg" viewBox="-220 0 1640 760" preserveAspectRatio="none" fill="none">
      {paths.map((path) => (
        <motion.path
          key={path.id}
          d={path.d}
          stroke="currentColor"
          strokeLinecap="round"
          strokeWidth={path.width}
          strokeOpacity={path.opacity}
          initial={{ pathLength: 0.25, opacity: path.opacity * 0.8 }}
          animate={{
            pathLength: 1,
            pathOffset: [0, 1],
            opacity: [path.opacity * 0.65, path.opacity, path.opacity * 0.65],
          }}
          transition={{
            duration: path.duration,
            repeat: Number.POSITIVE_INFINITY,
            ease: 'linear',
          }}
        />
      ))}
    </svg>
  )
}

export default function BackgroundPaths({ isDark = false, className = '', count = 28, variant = 'default' }) {
  const leftPaths = useMemo(() => buildPaths(1, count), [count])
  const rightPaths = useMemo(() => buildPaths(-1, count), [count])
  const cls = [
    'background-paths',
    isDark ? 'background-paths--dark' : 'background-paths--light',
    variant === 'strong' ? 'background-paths--strong' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div className={cls} aria-hidden>
      <FloatingPaths paths={leftPaths} />
      <FloatingPaths paths={rightPaths} />
      <div className="background-paths__veil" />
    </div>
  )
}
