import { useEffect, useRef } from 'react'
import { useMotionValue, useInView, animate } from 'motion/react'

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export default function Counter({
  from = 0,
  to,
  duration = 1.5,
  delay = 0,
  decimals = 0,
  prefix = '',
  suffix = '',
  className,
  style,
  once = true,
}) {
  const ref = useRef(null)
  const inView = useInView(ref, { once })
  const value = useMotionValue(from)
  const displayRef = useRef(null)

  useEffect(() => {
    if (!inView) return
    if (prefersReducedMotion()) {
      if (displayRef.current) displayRef.current.textContent = `${prefix}${to.toFixed(decimals)}${suffix}`
      return
    }
    const controls = animate(value, to, {
      duration,
      delay,
      ease: [0.16, 1, 0.3, 1],
      onUpdate: (v) => {
        if (displayRef.current) {
          displayRef.current.textContent = `${prefix}${v.toFixed(decimals)}${suffix}`
        }
      },
    })
    return () => controls.stop()
  }, [inView, to, from, duration, delay, decimals, prefix, suffix])

  return (
    <span ref={ref} className={className} style={style}>
      <span ref={displayRef}>{prefix}{from.toFixed(decimals)}{suffix}</span>
    </span>
  )
}
