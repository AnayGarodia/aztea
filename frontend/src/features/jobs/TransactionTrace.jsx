// OWNS: the canonical 8-step transaction trace surfaced on JobDetailPage
// NOT OWNS: cryptographic verification (lives in JobReceipt), rating UI, dispute flow
//
// INVARIANTS:
// - This component is read-only — it derives state from the job object and never mutates it.
// - Trust nodes (escrow opened, receipt signed, settled) render with --gold per DESIGN.md.
//   General-positive nodes (work delivered, reputation updated) stay sage / --positive.
// - Money strings always pass through fmtUsd; never raw cents in the DOM.
//
// DECISIONS:
// - Vertical layout: 8 steps will not fit horizontally on mobile or in a side rail.
// - We derive a state per node (future / active / done / failed / refunded / skipped) rather
//   than reading job.status directly in JSX, so the visual contract is one place.
import { Receipt, ShieldCheck, CircleDollarSign, Wallet, Bot, User, Coins, Star } from 'lucide-react'
import { fmtUsd } from '../../utils/format.js'
import './TransactionTrace.css'

const NODE_FUTURE   = 'future'
const NODE_ACTIVE   = 'active'
const NODE_DONE     = 'done'
const NODE_FAILED   = 'failed'
const NODE_REFUND   = 'refunded'
const NODE_SKIPPED  = 'skipped'

const TRUST_NODES = new Set(['escrow', 'receipt', 'settle'])

function statusBucket(jobStatus) {
  if (jobStatus === 'complete') return 'complete'
  if (jobStatus === 'failed') return 'failed'
  if (jobStatus === 'stopped') return 'failed'
  if (jobStatus === 'running' || jobStatus === 'claimed' || jobStatus === 'awaiting_clarification') return 'running'
  return 'pending'
}

function deriveNodes(job, agent) {
  const bucket = statusBucket(job?.status)
  const hasCharge = (job?.price_cents ?? 0) > 0
  const hasSignature = Boolean(job?.output_signature)
  const isRefunded = bucket === 'failed' && hasCharge
  const isRated = job?.caller_rating != null

  const escrowState = bucket === 'pending' ? NODE_ACTIVE
    : bucket === 'running' ? NODE_DONE
    : bucket === 'complete' ? NODE_DONE
    : isRefunded ? NODE_REFUND
    : NODE_FUTURE

  const deliveryState = bucket === 'pending' ? NODE_FUTURE
    : bucket === 'running' ? NODE_ACTIVE
    : bucket === 'complete' ? NODE_DONE
    : NODE_FAILED

  const receiptState = bucket !== 'complete' ? NODE_FUTURE
    : hasSignature ? NODE_DONE
    : NODE_SKIPPED

  const settleState = bucket === 'complete' ? NODE_DONE
    : isRefunded ? NODE_REFUND
    : NODE_FUTURE

  const reputationState = bucket !== 'complete' ? NODE_FUTURE
    : isRated ? NODE_DONE
    : NODE_ACTIVE

  return [
    { id: 'caller',    icon: User,             title: 'Caller',
      evidence: 'You',                                state: NODE_DONE },
    { id: 'specialist', icon: Bot,             title: 'Specialist',
      evidence: agent?.name || 'Unknown agent',       state: NODE_DONE },
    { id: 'cap',        icon: CircleDollarSign, title: 'Spend cap',
      evidence: hasCharge ? fmtUsd(job.price_cents) : '—', state: NODE_DONE },
    { id: 'escrow',     icon: Wallet,          title: 'Escrow opened',
      evidence: escrowState === NODE_REFUND ? 'Held → refunded'
              : escrowState === NODE_DONE   ? 'Held → released'
              : escrowState === NODE_ACTIVE ? 'Opening'
              : 'Pending',
      state: escrowState },
    { id: 'delivery',   icon: Receipt,         title: 'Work delivered',
      evidence: deliveryState === NODE_DONE   ? 'Output returned'
              : deliveryState === NODE_ACTIVE ? 'Running'
              : deliveryState === NODE_FAILED ? (job?.error_message || 'Failed')
              : 'Pending',
      state: deliveryState },
    { id: 'receipt',    icon: ShieldCheck,     title: 'Receipt signed',
      evidence: receiptState === NODE_DONE    ? 'Ed25519 · verify below'
              : receiptState === NODE_SKIPPED ? 'No signature on this job'
              : 'Awaiting completion',
      state: receiptState },
    { id: 'settle',     icon: Coins,           title: 'Settled',
      evidence: settleState === NODE_REFUND   ? `${fmtUsd(job.price_cents)} refunded · platform earned $0`
              : settleState === NODE_DONE     ? `90% to builder · 10% platform fee`
              : 'Pending outcome',
      state: settleState },
    { id: 'reputation', icon: Star,            title: 'Reputation updated',
      evidence: reputationState === NODE_DONE   ? `You rated ${job.caller_rating}/5`
              : reputationState === NODE_ACTIVE ? 'Awaiting your rating'
              : 'Locked until delivery',
      state: reputationState },
  ]
}

function NodeMarker({ icon: Icon, state, isTrust }) {
  const cls = [
    'tt__marker',
    `tt__marker--${state}`,
    isTrust && state === NODE_DONE ? 'tt__marker--trust' : '',
  ].filter(Boolean).join(' ')
  return (
    <div className={cls} aria-hidden="true">
      <Icon size={13} strokeWidth={2.1} />
    </div>
  )
}

const STATE_LABEL = {
  [NODE_FUTURE]:  'pending',
  [NODE_ACTIVE]:  'in progress',
  [NODE_DONE]:    'done',
  [NODE_FAILED]:  'failed',
  [NODE_REFUND]:  'refunded',
  [NODE_SKIPPED]: 'not applicable',
}

export default function TransactionTrace({ job, agent }) {
  if (!job) return null
  const nodes = deriveNodes(job, agent)

  return (
    <ol className="tt" aria-label="Transaction trace">
      {nodes.map((n, i) => {
        const isTrust = TRUST_NODES.has(n.id) && n.state === NODE_DONE
        return (
          <li key={n.id} className={`tt__row tt__row--${n.state}`}>
            <div className="tt__rail" aria-hidden="true">
              <NodeMarker icon={n.icon} state={n.state} isTrust={isTrust} />
              {i < nodes.length - 1 && <span className="tt__line" />}
            </div>
            <div className="tt__body">
              <p className="tt__title">{n.title}</p>
              <p className={`tt__evidence${isTrust ? ' tt__evidence--trust' : ''}`}>{n.evidence}</p>
            </div>
            <span className="tt__state-label" aria-label={`Status: ${STATE_LABEL[n.state]}`}>
              {STATE_LABEL[n.state]}
            </span>
          </li>
        )
      })}
    </ol>
  )
}
