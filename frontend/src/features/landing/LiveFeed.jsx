import { useEffect, useRef, useState } from 'react'
import './LiveFeed.css'

const SEED_EVENTS = [
  { agent: 'Financial Research', detail: 'AAPL · 10-Q analysis', cost: '$0.01', status: 'complete', ms: 2100 },
  { agent: 'Code Review',         detail: 'auth_middleware.py',   cost: '$0.02', status: 'complete', ms: 3400 },
  { agent: 'Text Intelligence',   detail: 'earnings call Q3',     cost: '$0.01', status: 'complete', ms: 1800 },
  { agent: 'Financial Research', detail: 'MSFT · 10-K filing',   cost: '$0.01', status: 'complete', ms: 2600 },
  { agent: 'Code Review',         detail: 'payments.py security', cost: '$0.02', status: 'complete', ms: 4100 },
  { agent: 'Text Intelligence',   detail: 'Reddit thread scrape', cost: '$0.01', status: 'complete', ms: 1300 },
  { agent: 'Financial Research', detail: 'NVDA · risk factors',   cost: '$0.01', status: 'complete', ms: 2900 },
  { agent: 'Code Review',         detail: 'inference_server.go',  cost: '$0.02', status: 'complete', ms: 3700 },
  { agent: 'Text Intelligence',   detail: 'product reviews NLP',  cost: '$0.01', status: 'complete', ms: 1600 },
  { agent: 'Financial Research', detail: 'TSLA · 10-Q signals',   cost: '$0.01', status: 'complete', ms: 2200 },
  { agent: 'Code Review',         detail: 'scheduler.rs review',  cost: '$0.02', status: 'complete', ms: 3100 },
  { agent: 'Text Intelligence',   detail: 'customer feedback',    cost: '$0.01', status: 'complete', ms: 1900 },
]

function ago(secs) {
  if (secs < 60) return `${secs}s ago`
  return `${Math.floor(secs / 60)}m ago`
}

function FeedItem({ ev, style }) {
  return (
    <div className="livefeed__item" style={style}>
      <span className="livefeed__dot livefeed__dot--ok" />
      <span className="livefeed__agent">{ev.agent}</span>
      <span className="livefeed__sep">·</span>
      <span className="livefeed__detail">{ev.detail}</span>
      <span className="livefeed__sep">·</span>
      <span className="livefeed__cost">{ev.cost}</span>
      <span className="livefeed__sep">·</span>
      <span className="livefeed__ms">{(ev.ms / 1000).toFixed(1)}s</span>
      <span className="livefeed__sep">·</span>
      <span className="livefeed__time">{ago(Math.floor(Math.random() * 55 + 2))}</span>
    </div>
  )
}

export default function LiveFeed({ liveAgents = [] }) {
  // Merge real agent names into the seed data
  const items = (() => {
    const base = [...SEED_EVENTS]
    if (liveAgents.length > 0) {
      liveAgents.slice(0, 3).forEach(a => {
        base.unshift({
          agent: a.name,
          detail: `live call`,
          cost: `$${Number(a.price_per_call_usd || 0).toFixed(2)}`,
          status: 'complete',
          ms: Math.round((a.avg_latency_ms ?? 2000)),
        })
      })
    }
    // Duplicate for seamless loop
    return [...base, ...base]
  })()

  return (
    <div className="livefeed" aria-hidden="true">
      <div className="livefeed__label">live</div>
      <div className="livefeed__track-wrap">
        <div className="livefeed__fade livefeed__fade--left" />
        <div className="livefeed__track">
          <div className="livefeed__scroll">
            {items.map((ev, i) => <FeedItem key={i} ev={ev} />)}
          </div>
        </div>
        <div className="livefeed__fade livefeed__fade--right" />
      </div>
    </div>
  )
}
