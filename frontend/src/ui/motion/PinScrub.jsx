import { useScroll } from 'motion/react'
import { useRef } from 'react'
import useReducedMotion from '../../utils/useReducedMotion'

export default function PinScrub({
  children,
  heightVh = 200,
  className,
  innerClassName,
  style,
  id,
  justifyContent = 'center',
}) {
  const ref = useRef(null)
  const reduced = useReducedMotion()
  const { scrollYProgress } = useScroll({
    target: ref,
    offset: ['start start', 'end end'],
  })

  if (reduced) {
    return (
      <section ref={ref} id={id} className={className} style={style}>
        <div className={innerClassName}>{children(null)}</div>
      </section>
    )
  }

  return (
    <section
      ref={ref}
      id={id}
      className={className}
      style={{ ...style, height: `${heightVh}vh`, position: 'relative' }}
    >
      <div
        className={innerClassName}
        style={{
          position: 'sticky',
          top: 0,
          height: '100vh',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          justifyContent,
        }}
      >
        {children(scrollYProgress)}
      </div>
    </section>
  )
}
