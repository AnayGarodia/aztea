import { useRef } from 'react'
import { motion, useInView } from 'motion/react'
import { easeExpo } from '../../theme/motion'

const reduced =
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export function AnimatedPath({
  d,
  duration = 1.2,
  delay = 0,
  stroke = 'var(--accent)',
  strokeWidth = 2,
  inView = true,
}) {
  return (
    <motion.path
      d={d}
      stroke={stroke}
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      fill="none"
      initial={reduced ? { pathLength: 1, opacity: 1 } : { pathLength: 0, opacity: 0 }}
      animate={inView ? { pathLength: 1, opacity: 1 } : { pathLength: 0, opacity: 0 }}
      transition={{
        pathLength: { duration, delay, ease: easeExpo },
        opacity: { duration: 0.2, delay },
      }}
    />
  )
}

export default function PathDraw({
  d,
  duration = 1.2,
  delay = 0,
  stroke = 'var(--accent)',
  strokeWidth = 2,
  viewBox,
  width,
  height,
  className,
  style,
  children,
}) {
  const ref = useRef(null)
  const inView = useInView(ref, { once: true })

  return (
    <svg
      ref={ref}
      viewBox={viewBox}
      width={width}
      height={height}
      className={className}
      style={style}
      fill="none"
      aria-hidden
    >
      {children}
      <AnimatedPath
        d={d}
        duration={duration}
        delay={delay}
        stroke={stroke}
        strokeWidth={strokeWidth}
        inView={inView}
      />
    </svg>
  )
}
