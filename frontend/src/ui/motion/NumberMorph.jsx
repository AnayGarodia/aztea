import { useEffect, useRef, useState } from 'react'
import { useInView } from 'motion/react'

const reduced =
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

function easeOutExpo(t) {
  return t === 1 ? 1 : 1 - Math.pow(2, -10 * t)
}

export default function NumberMorph({
  value,
  decimals = 0,
  prefix = '',
  suffix = '',
  duration = 0.7,
  placeholder = '-',
  className,
  style,
}) {
  const ref = useRef(null)
  const inView = useInView(ref, { once: true })
  const frameRef = useRef(null)
  const fromRef = useRef(0)
  const [displayed, setDisplayed] = useState(null)

  useEffect(() => {
    if (value == null || typeof value !== 'number') return
    const target = value

    if (reduced || !inView) {
      setDisplayed(target)
      fromRef.current = target
      return
    }

    cancelAnimationFrame(frameRef.current)
    const from = fromRef.current
    const start = performance.now()
    const dur = duration * 1000

    const tick = (now) => {
      const t = Math.min((now - start) / dur, 1)
      const eased = easeOutExpo(t)
      setDisplayed(from + (target - from) * eased)
      if (t < 1) {
        frameRef.current = requestAnimationFrame(tick)
      } else {
        fromRef.current = target
        setDisplayed(target)
      }
    }

    frameRef.current = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frameRef.current)
  }, [value, inView]) // eslint-disable-line

  if (displayed == null) {
    return <span ref={ref} className={className} style={style}>{placeholder}</span>
  }

  const fmt = displayed.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })

  return (
    <span ref={ref} className={className} style={style}>
      {prefix}{fmt}{suffix}
    </span>
  )
}
