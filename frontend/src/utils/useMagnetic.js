import { useEffect, useRef } from 'react'
import { useMotionValue, useSpring } from 'motion/react'
import useReducedMotion from './useReducedMotion'

const SPRING_CONFIG = { stiffness: 200, damping: 25, mass: 0.5 }
const MAX_OFFSET_PX = 10

export default function useMagnetic({ strength = 0.25 } = {}) {
  const ref = useRef(null)
  const reduced = useReducedMotion()
  const x = useMotionValue(0)
  const y = useMotionValue(0)
  const xSpring = useSpring(x, SPRING_CONFIG)
  const ySpring = useSpring(y, SPRING_CONFIG)

  useEffect(() => {
    if (reduced) return
    const el = ref.current
    if (!el) return

    const onPointerMove = (e) => {
      const rect = el.getBoundingClientRect()
      const cx = rect.left + rect.width / 2
      const cy = rect.top + rect.height / 2
      const dx = (e.clientX - cx) * strength
      const dy = (e.clientY - cy) * strength
      x.set(clamp(dx, -MAX_OFFSET_PX, MAX_OFFSET_PX))
      y.set(clamp(dy, -MAX_OFFSET_PX, MAX_OFFSET_PX))
    }
    const onPointerLeave = () => {
      x.set(0)
      y.set(0)
    }

    el.addEventListener('pointermove', onPointerMove)
    el.addEventListener('pointerleave', onPointerLeave)
    return () => {
      el.removeEventListener('pointermove', onPointerMove)
      el.removeEventListener('pointerleave', onPointerLeave)
    }
  }, [reduced, strength, x, y])

  return { ref, x: xSpring, y: ySpring, reduced }
}

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v))
}
