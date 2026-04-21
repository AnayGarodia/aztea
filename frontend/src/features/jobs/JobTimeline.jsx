import { motion } from 'motion/react'
import './JobTimeline.css'

const STATUS_FLOW = ['pending', 'running', 'awaiting_clarification', 'complete']

const LABELS = {
  pending:                 'Queued',
  running:                 'Running',
  awaiting_clarification:  'Needs input',
  complete:                'Done',
  failed:                  'Failed',
}

function deriveNodes(status) {
  if (status === 'awaiting_clarification') return STATUS_FLOW
  return ['pending', 'running', 'complete']
}

function fmtTs(isoString) {
  if (!isoString) return null
  return new Date(isoString).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export default function JobTimeline({ status, timestamps = {} }) {
  if (!status) return null

  const isFailed  = status === 'failed'
  const nodes     = isFailed ? ['pending', 'running', 'failed'] : deriveNodes(status)
  const activeIdx = isFailed ? nodes.length - 1 : nodes.indexOf(status)

  return (
    <div className="jtl" role="status" aria-label={`Job status: ${status}`}>
      {nodes.map((node, i) => {
        const isPast    = i < activeIdx
        const isActive  = i === activeIdx
        const isFuture  = i > activeIdx
        const isFailNode = node === 'failed'

        return (
          <div key={node} className="jtl__step">
            {/* Connector line before this node */}
            {i > 0 && (
              <div className={`jtl__line ${isPast || isActive ? 'jtl__line--filled' : ''} ${isFailNode ? 'jtl__line--fail' : ''}`}>
                {(isPast || isActive) && (
                  <motion.div
                    className={`jtl__line-fill ${isFailNode ? 'jtl__line-fill--fail' : ''}`}
                    initial={{ scaleX: 0 }}
                    animate={{ scaleX: 1 }}
                    transition={{ duration: 0.4, ease: [0.2, 0.8, 0.2, 1] }}
                  />
                )}
              </div>
            )}

            {/* Dot */}
            <div className={[
              'jtl__dot',
              isPast   ? 'jtl__dot--past'   : '',
              isActive ? 'jtl__dot--active'  : '',
              isFuture ? 'jtl__dot--future'  : '',
              isFailNode ? 'jtl__dot--fail'  : '',
            ].filter(Boolean).join(' ')}>
              {isPast && !isFailNode && (
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                  <polyline points="1.5,5 4,7.5 8.5,2.5" stroke="white" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              )}
              {isActive && !isFailNode && (
                <motion.div
                  className="jtl__pulse"
                  animate={{ scale: [1, 1.5, 1], opacity: [0.6, 0, 0.6] }}
                  transition={{ duration: 1.8, repeat: Infinity, ease: 'easeInOut' }}
                />
              )}
              {isFailNode && isActive && (
                <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                  <line x1="2" y1="2" x2="8" y2="8" stroke="white" strokeWidth="1.5" strokeLinecap="round" />
                  <line x1="8" y1="2" x2="2" y2="8" stroke="white" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              )}
            </div>

            {/* Label + timestamp */}
            <div className="jtl__label-wrap">
              <span className={[
                'jtl__label',
                isPast   ? 'jtl__label--past'   : '',
                isActive ? 'jtl__label--active'  : '',
                isFuture ? 'jtl__label--future'  : '',
                isFailNode ? 'jtl__label--fail'  : '',
              ].filter(Boolean).join(' ')}>
                {LABELS[node] ?? node}
              </span>
              {fmtTs(timestamps[node]) && (isPast || isActive) && (
                <span className="jtl__ts">{fmtTs(timestamps[node])}</span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
