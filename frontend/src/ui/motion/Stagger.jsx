import { motion, useInView } from 'motion/react'
import { useRef } from 'react'

const prefersReducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export default function Stagger({
  children,
  staggerDelay = 0.08,
  delayStart = 0,
  y = 16,
  once = true,
  className,
  style,
}) {
  const ref = useRef(null)
  const inView = useInView(ref, { once, margin: '-10% 0px' })
  const reduced = prefersReducedMotion()

  const container = {
    hidden: {},
    show: {
      transition: {
        staggerChildren: staggerDelay,
        delayChildren: delayStart,
      },
    },
  }

  const item = {
    hidden: reduced ? {} : { opacity: 0, y },
    show: { opacity: 1, y: 0, transition: { duration: 0.45, ease: [0.16, 1, 0.3, 1] } },
  }

  return (
    <motion.div
      ref={ref}
      className={className}
      style={style}
      variants={container}
      initial="hidden"
      animate={inView ? 'show' : 'hidden'}
    >
      {Array.isArray(children)
        ? children.map((child, i) => (
            <motion.div key={i} variants={item} style={{ display: 'contents' }}>
              {child}
            </motion.div>
          ))
        : <motion.div variants={item} style={{ display: 'contents' }}>{children}</motion.div>
      }
    </motion.div>
  )
}
