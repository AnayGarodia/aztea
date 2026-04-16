import { motion } from 'motion/react'

/**
 * Animated active indicator using shared layoutId.
 * Usage: render inside the active item with a consistent layoutId.
 */
export default function LayoutPill({ layoutId = 'layout-pill', className, style }) {
  return (
    <motion.span
      layoutId={layoutId}
      className={className}
      style={style}
      transition={{ type: 'spring', bounce: 0.2, duration: 0.35 }}
    />
  )
}
