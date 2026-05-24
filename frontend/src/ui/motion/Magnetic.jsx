import { motion } from 'motion/react'
import useMagnetic from '../../utils/useMagnetic'

export default function Magnetic({
  children,
  strength = 0.25,
  className,
  style,
  as = 'div',
}) {
  const { ref, x, y, reduced } = useMagnetic({ strength })
  const Component = motion[as] || motion.div

  if (reduced) {
    return <div ref={ref} className={className} style={style}>{children}</div>
  }

  return (
    <Component ref={ref} className={className} style={{ ...style, x, y, display: 'inline-block' }}>
      {children}
    </Component>
  )
}
