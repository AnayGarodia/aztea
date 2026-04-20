import { useEffect, useRef, useState } from 'react'
import './ContainerScroll.css'

function clamp01(value) {
  if (value < 0) return 0
  if (value > 1) return 1
  return value
}

function Header({ translateY, titleComponent }) {
  return (
    <div
      style={{ transform: `translateY(${translateY}px)` }}
      className="container-scroll__header"
    >
      {titleComponent}
    </div>
  )
}

function Card({ rotateX, scale, children }) {
  return (
    <div
      style={{
        transform: `rotateX(${rotateX}deg) scale(${scale})`,
        boxShadow:
          '0 16px 24px rgba(0, 0, 0, 0.09), 0 42px 65px rgba(0, 0, 0, 0.08), 0 3px 16px rgba(0, 0, 0, 0.05)',
      }}
      className="container-scroll__card"
    >
      <div className="container-scroll__card-inner">
        {children}
      </div>
    </div>
  )
}

export default function ContainerScroll({ titleComponent, children, className = '' }) {
  const containerRef = useRef(null)
  const [isMobile, setIsMobile] = useState(false)
  const [progress, setProgress] = useState(0)

  useEffect(() => {
    const checkMobile = () => setIsMobile(window.innerWidth <= 768)
    checkMobile()
    window.addEventListener('resize', checkMobile)
    return () => window.removeEventListener('resize', checkMobile)
  }, [])

  useEffect(() => {
    let frame = 0
    const measure = () => {
      const node = containerRef.current
      if (!node) return
      const rect = node.getBoundingClientRect()
      const viewportH = window.innerHeight
      const start = viewportH * 0.9
      const end = -rect.height * 0.35
      const raw = (start - rect.top) / Math.max(start - end, 1)
      setProgress(clamp01(raw))
    }

    const onScroll = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(measure)
    }

    measure()
    window.addEventListener('scroll', onScroll, { passive: true })
    document.addEventListener('scroll', onScroll, { passive: true, capture: true })
    window.addEventListener('resize', onScroll)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('scroll', onScroll)
      document.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onScroll)
    }
  }, [])

  const rotateX = isMobile ? 18 - progress * 18 : 24 - progress * 24
  const startScale = isMobile ? 0.88 : 1.12
  const scale = startScale + (1 - startScale) * progress
  const translateY = isMobile ? 54 - progress * 82 : 84 - progress * 122
  const cls = ['container-scroll', className].filter(Boolean).join(' ')
  const content = typeof children === 'function' ? children(progress) : children

  return (
    <div className={cls} ref={containerRef}>
      <div className="container-scroll__inner">
        <Header translateY={translateY} titleComponent={titleComponent} />
        <Card rotateX={rotateX} scale={scale}>
          {content}
        </Card>
      </div>
    </div>
  )
}
