import { useRef } from 'react'
import './Marquee.css'

export default function Marquee({ children, speed = 40, gap = 32, className, reverse = false }) {
  return (
    <div className={`marquee ${className ?? ''}`} style={{ '--marquee-gap': `${gap}px` }}>
      <div
        className="marquee__track"
        style={{
          animationDuration: `${speed}s`,
          animationDirection: reverse ? 'reverse' : 'normal',
        }}
      >
        <div className="marquee__content">{children}</div>
        <div className="marquee__content" aria-hidden>{children}</div>
      </div>
    </div>
  )
}
