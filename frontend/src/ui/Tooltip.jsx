import { useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { easeExpo } from '../theme/motion'
import './Tooltip.css'

export default function Tooltip({ label, side = 'top', children, className }) {
  const [open, setOpen] = useState(false)

  const initial = { opacity: 0, scale: 0.92, y: side === 'top' ? 4 : side === 'bottom' ? -4 : 0 }
  const animate = { opacity: 1, scale: 1, y: 0 }
  const exit    = { opacity: 0, scale: 0.92, y: side === 'top' ? 4 : side === 'bottom' ? -4 : 0 }

  return (
    <span
      className={`tooltip-wrap ${className ?? ''}`}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {children}
      <AnimatePresence>
        {open && label && (
          <motion.span
            role="tooltip"
            className={`tooltip tooltip--${side}`}
            initial={initial}
            animate={animate}
            exit={exit}
            transition={{ duration: 0.15, ease: easeExpo }}
          >
            {label}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  )
}
