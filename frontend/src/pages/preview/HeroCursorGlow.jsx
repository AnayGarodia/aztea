import { useEffect, useRef } from 'react'
import { motion, useMotionValue, useSpring, useMotionTemplate } from 'motion/react'
import useReducedMotion from '../../utils/useReducedMotion'
import './HeroCursorGlow.css'

const SPRING = { stiffness: 200, damping: 25, mass: 0.5 }

export default function HeroCursorGlow() {
  const reduced = useReducedMotion()
  const ref = useRef(null)
  const x = useMotionValue(50)
  const y = useMotionValue(50)
  const xS = useSpring(x, SPRING)
  const yS = useSpring(y, SPRING)
  const background = useMotionTemplate`radial-gradient(circle 480px at ${xS}% ${yS}%, var(--terracotta-bg), transparent 65%)`

  useEffect(() => {
    if (reduced) return
    const el = ref.current
    if (!el) return
    const onMove = (e) => {
      const rect = el.getBoundingClientRect()
      x.set(((e.clientX - rect.left) / rect.width) * 100)
      y.set(((e.clientY - rect.top) / rect.height) * 100)
    }
    el.addEventListener('pointermove', onMove)
    return () => el.removeEventListener('pointermove', onMove)
  }, [reduced, x, y])

  if (reduced) {
    return (
      <div ref={ref} className="hcg">
        <div className="hcg__layer hcg__layer--static" />
      </div>
    )
  }

  return (
    <div ref={ref} className="hcg">
      <motion.div className="hcg__layer" style={{ background }} />
    </div>
  )
}
