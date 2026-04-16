import { motion, useInView } from 'motion/react'
import { useRef } from 'react'

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export default function Reveal({
  children,
  delay = 0,
  duration = 0.5,
  y = 20,
  x = 0,
  once = true,
  className,
  style,
  as: Tag = 'div',
}) {
  const ref = useRef(null)
  const inView = useInView(ref, { once, margin: '-10% 0px' })
  const reduced = prefersReducedMotion()

  return (
    <motion.div
      ref={ref}
      className={className}
      style={style}
      initial={reduced ? false : { opacity: 0, y, x }}
      animate={inView ? { opacity: 1, y: 0, x: 0 } : {}}
      transition={{ duration, delay, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  )
}
