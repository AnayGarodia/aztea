import { motion, useScroll, useTransform } from 'motion/react'
import { useRef } from 'react'
import useReducedMotion from '../../utils/useReducedMotion'

export default function Parallax({
  children,
  range = 40,
  className,
  style,
  as: Tag = motion.div,
}) {
  const ref = useRef(null)
  const reduced = useReducedMotion()
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ['start end', 'end start'],
  })
  const y = useTransform(scrollYProgress, [0, 1], [range, -range])

  if (reduced) {
    return <div ref={ref} className={className} style={style}>{children}</div>
  }

  return (
    <Tag ref={ref} className={className} style={{ ...style, y }}>
      {children}
    </Tag>
  )
}
