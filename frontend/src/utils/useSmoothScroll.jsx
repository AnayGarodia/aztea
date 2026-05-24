import { createContext, useContext, useEffect, useRef, useState } from 'react'
import Lenis from 'lenis'
import 'lenis/dist/lenis.css'
import useReducedMotion from './useReducedMotion'

const LenisContext = createContext(null)

const LENIS_DURATION = 1.2
const EASE_OUT_CUBIC = (t) => 1 - Math.pow(1 - t, 3)

function isCoarsePointer() {
  if (typeof window === 'undefined') return false
  return window.matchMedia('(pointer: coarse)').matches
}

export function SmoothScrollProvider({ children }) {
  const reduced = useReducedMotion()
  const [lenis, setLenis] = useState(null)
  const rafRef = useRef(0)

  useEffect(() => {
    if (reduced || isCoarsePointer()) {
      setLenis(null)
      return
    }
    const instance = new Lenis({
      duration: LENIS_DURATION,
      easing: EASE_OUT_CUBIC,
      smoothWheel: true,
    })
    const raf = (time) => {
      instance.raf(time)
      rafRef.current = requestAnimationFrame(raf)
    }
    rafRef.current = requestAnimationFrame(raf)
    setLenis(instance)
    return () => {
      cancelAnimationFrame(rafRef.current)
      instance.destroy()
      setLenis(null)
    }
  }, [reduced])

  return <LenisContext.Provider value={lenis}>{children}</LenisContext.Provider>
}

export function useLenis() {
  return useContext(LenisContext)
}

export function scrollToTarget(lenis, target, options = {}) {
  if (lenis) {
    lenis.scrollTo(target, { duration: LENIS_DURATION, ...options })
    return
  }
  const el = typeof target === 'string'
    ? document.querySelector(target)
    : target
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
