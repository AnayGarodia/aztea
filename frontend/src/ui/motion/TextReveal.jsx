import { motion, useInView } from 'motion/react'
import { useMemo, useRef } from 'react'
import useReducedMotion from '../../utils/useReducedMotion'

const EASE_OUT_EXPO = [0.19, 1, 0.22, 1]

function splitIntoWords(text) {
  return text.split(/(\s+)/).filter(Boolean)
}

export default function TextReveal({
  text,
  as: Tag = 'span',
  stagger = 0.04,
  duration = 0.5,
  delay = 0,
  once = true,
  className,
  style,
  inline = false,
}) {
  const ref = useRef(null)
  const inView = useInView(ref, { once, margin: '-15% 0px' })
  const reduced = useReducedMotion()
  const tokens = useMemo(() => splitIntoWords(text || ''), [text])

  if (reduced) {
    return <Tag ref={ref} className={className} style={style}>{text}</Tag>
  }

  return (
    <Tag ref={ref} className={className} style={style}>
      {tokens.map((token, i) => {
        if (/^\s+$/.test(token)) return <span key={i}>{token}</span>
        return (
          <span
            key={i}
            className="text-reveal-mask"
            style={{
              display: inline ? 'inline-block' : 'inline-block',
              overflow: 'hidden',
              verticalAlign: 'bottom',
            }}
          >
            <motion.span
              style={{ display: 'inline-block' }}
              initial={{ y: '110%' }}
              animate={inView ? { y: '0%' } : { y: '110%' }}
              transition={{
                duration,
                delay: delay + i * stagger,
                ease: EASE_OUT_EXPO,
              }}
            >
              {token}
            </motion.span>
          </span>
        )
      })}
    </Tag>
  )
}
