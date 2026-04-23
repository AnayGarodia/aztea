import { useMemo } from 'react'
import './ResultRenderer.css'

// ── Helpers ───────────────────────────────────────────────────────────────

const SUMMARY_KEYS = ['summary', 'message', 'answer', 'title', 'one_line_summary', 'description', 'conclusion']
const ARTIFACT_KEYS = ['artifacts', 'output_artifacts']

const URL_RE = /https?:\/\/[^\s<>"')\]]+/gi
const ISO_LIKE_RE = /^\d{4}-\d{2}-\d{2}(?:[T ][\d:.+Z-]+)?$/
const UNIX_TS_RE = /^1\d{9,12}$/

function titleize(key) {
  return String(key)
    .replace(/[_-]+/g, ' ')
    .replace(/\b([a-z])/g, (_, c) => c.toUpperCase())
}

function isPrimitive(value) {
  return value == null || ['string', 'number', 'boolean'].includes(typeof value)
}

function isPlainObject(value) {
  return value != null && typeof value === 'object' && !Array.isArray(value)
}

function tryParseDate(raw) {
  if (raw == null) return null
  if (typeof raw === 'number') {
    const ms = raw < 1e12 ? raw * 1000 : raw
    const d = new Date(ms)
    if (!Number.isNaN(d.getTime())) return d
    return null
  }
  if (typeof raw !== 'string') return null
  const trimmed = raw.trim()
  if (!trimmed) return null
  if (UNIX_TS_RE.test(trimmed)) {
    const n = Number(trimmed)
    const ms = n < 1e12 ? n * 1000 : n
    const d = new Date(ms)
    return Number.isNaN(d.getTime()) ? null : d
  }
  if (!ISO_LIKE_RE.test(trimmed)) return null
  const d = new Date(trimmed)
  return Number.isNaN(d.getTime()) ? null : d
}

function formatDate(d) {
  try {
    return d.toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  } catch {
    return d.toISOString()
  }
}

function isoFor(d) {
  try { return d.toISOString() } catch { return '' }
}

function isTimestampKey(key) {
  const k = String(key).toLowerCase()
  return /(?:_at|_time|timestamp|_ts|datetime)$/.test(k) || k === 'created_at' || k === 'updated_at'
}

function isUrl(value) {
  if (typeof value !== 'string') return false
  const s = value.trim()
  return /^https?:\/\//i.test(s)
}

function isHostedImage(value) {
  if (!isUrl(value)) return false
  return /\.(?:png|jpe?g|gif|webp|svg|avif)(?:\?|#|$)/i.test(value)
}

function isDataImage(value) {
  return typeof value === 'string' && /^data:image\//i.test(value.trim())
}

function asDisplaySrc(mime, value) {
  const raw = String(value ?? '').trim()
  if (!raw) return null
  if (raw.startsWith('data:') || raw.startsWith('http://') || raw.startsWith('https://')) return raw
  if (raw.startsWith('/')) return raw
  if (String(mime ?? '').toLowerCase().includes('/')) {
    return `data:${mime};base64,${raw}`
  }
  return null
}

function formatNumber(n) {
  if (typeof n !== 'number' || Number.isNaN(n)) return String(n)
  if (!Number.isFinite(n)) return String(n)
  if (Number.isInteger(n) && Math.abs(n) >= 1000) return n.toLocaleString()
  if (Math.abs(n) > 0 && Math.abs(n) < 0.0001) return n.toExponential(3)
  return String(n)
}

// ── Inline primitives ────────────────────────────────────────────────────

function LinkedText({ text }) {
  const str = String(text ?? '')
  if (!str) return null
  const matches = Array.from(str.matchAll(URL_RE))
  if (matches.length === 0) return str
  const nodes = []
  let cursor = 0
  for (const m of matches) {
    const start = m.index ?? 0
    const url = m[0]
    if (start > cursor) nodes.push(str.slice(cursor, start))
    nodes.push(
      <a
        key={`${url}-${start}`}
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="result-link"
      >{url}</a>
    )
    cursor = start + url.length
  }
  if (cursor < str.length) nodes.push(str.slice(cursor))
  return <>{nodes.map((n, i) => <span key={i}>{n}</span>)}</>
}

function PrimitiveValue({ value, hintKey }) {
  if (value == null) return <span className="result-generic-row__null">—</span>
  if (typeof value === 'boolean') {
    return <span className={`result-pill ${value ? 'result-pill--pos' : 'result-pill--neg'}`}>{value ? 'true' : 'false'}</span>
  }
  if (typeof value === 'number') {
    return <span className="result-generic-row__value result-generic-row__value--mono">{formatNumber(value)}</span>
  }
  const str = String(value)
  if (hintKey && isTimestampKey(hintKey)) {
    const d = tryParseDate(str) ?? tryParseDate(Number(str))
    if (d) {
      return (
        <time dateTime={isoFor(d)} className="result-generic-row__value result-generic-row__value--mono">
          {formatDate(d)}
        </time>
      )
    }
  }
  const d = tryParseDate(str)
  if (d && str.length >= 10) {
    return (
      <time dateTime={isoFor(d)} className="result-generic-row__value result-generic-row__value--mono">
        {formatDate(d)}
      </time>
    )
  }
  if (isHostedImage(str) || isDataImage(str)) {
    return (
      <a href={str} target="_blank" rel="noopener noreferrer" className="result-generic-row__value">
        <img className="result-inline-image" src={str} alt="" loading="lazy" />
      </a>
    )
  }
  if (isUrl(str)) {
    return <a href={str} target="_blank" rel="noopener noreferrer" className="result-link">{str}</a>
  }
  if (str.length > 220 || str.includes('\n')) {
    return (
      <p className="result-generic-row__value result-generic-row__value--paragraph">
        <LinkedText text={str} />
      </p>
    )
  }
  return (
    <span className="result-generic-row__value">
      <LinkedText text={str} />
    </span>
  )
}

function ListOfPrimitives({ items }) {
  return (
    <ul className="result-generic-list">
      {items.map((item, i) => (
        <li key={i}><PrimitiveValue value={item} /></li>
      ))}
    </ul>
  )
}

function uniformObjectKeys(arr) {
  if (!Array.isArray(arr) || arr.length === 0) return null
  if (!arr.every(isPlainObject)) return null
  const first = Object.keys(arr[0])
  if (!first.length || first.length > 8) return null
  const matches = arr.every(obj => {
    const keys = Object.keys(obj)
    if (keys.length !== first.length) return false
    return first.every(k => keys.includes(k))
  })
  return matches ? first : null
}

function ObjectTable({ rows, columns }) {
  return (
    <div className="result-table-wrap">
      <table className="result-table">
        <thead>
          <tr>{columns.map(col => <th key={col} scope="col">{titleize(col)}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map(col => (
                <td key={col}>
                  {isPrimitive(row[col])
                    ? <PrimitiveValue value={row[col]} hintKey={col} />
                    : <CompactValue value={row[col]} />}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ObjectCardGrid({ items }) {
  return (
    <div className="result-card-grid">
      {items.map((obj, i) => (
        <div key={i} className="result-card">
          <KeyValueBlock value={obj} compact />
        </div>
      ))}
    </div>
  )
}

function KeyValueBlock({ value, compact = false, depth = 0 }) {
  const entries = Object.entries(value)
  if (entries.length === 0) return <p className="result-generic-row__null">Empty</p>
  return (
    <div className={`result-kv${compact ? ' result-kv--compact' : ''}`}>
      {entries.map(([k, v]) => (
        <div key={k} className="result-kv__row">
          <p className="result-kv__key">{titleize(k)}</p>
          {isPrimitive(v) ? (
            <PrimitiveValue value={v} hintKey={k} />
          ) : Array.isArray(v) ? (
            <ArrayValue items={v} depth={depth + 1} />
          ) : (
            <NestedObject value={v} depth={depth + 1} />
          )}
        </div>
      ))}
    </div>
  )
}

function NestedObject({ value, depth }) {
  if (depth > 3) {
    return <pre className="result-json">{JSON.stringify(value, null, 2)}</pre>
  }
  return (
    <div className="result-nested">
      <KeyValueBlock value={value} compact depth={depth} />
    </div>
  )
}

function ArrayValue({ items, depth = 0 }) {
  if (items.length === 0) return <span className="result-generic-row__null">[]</span>
  if (items.every(isPrimitive)) return <ListOfPrimitives items={items} />
  const cols = uniformObjectKeys(items)
  if (cols) return <ObjectTable rows={items} columns={cols} />
  if (items.every(isPlainObject)) return <ObjectCardGrid items={items} />
  if (depth > 3) return <pre className="result-json">{JSON.stringify(items, null, 2)}</pre>
  return (
    <div className="result-card-grid">
      {items.map((item, i) => (
        <div key={i} className="result-card">
          <CompactValue value={item} />
        </div>
      ))}
    </div>
  )
}

function CompactValue({ value }) {
  if (isPrimitive(value)) return <PrimitiveValue value={value} />
  if (Array.isArray(value)) return <ArrayValue items={value} />
  if (isPlainObject(value)) return <KeyValueBlock value={value} compact />
  return <pre className="result-json">{JSON.stringify(value, null, 2)}</pre>
}

// ── Artifacts ────────────────────────────────────────────────────────────

function ArtifactCard({ artifact }) {
  const name = String(artifact.name ?? 'artifact')
  const mime = String(artifact.mime ?? '').toLowerCase()
  const source = asDisplaySrc(mime, artifact.url_or_base64)
  const size = Number(artifact.size_bytes ?? 0)
  const sizeLabel = size > 0
    ? (size < 1024 ? `${size} B`
      : size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KB`
        : `${(size / 1024 / 1024).toFixed(1)} MB`)
    : null

  const isImage = mime.startsWith('image/') || (source && /\.(png|jpe?g|gif|webp|svg|avif)(?:\?|#|$)/i.test(source))
  const isVideo = mime.startsWith('video/') || (source && /\.(mp4|webm|mov)(?:\?|#|$)/i.test(source))
  const isAudio = mime.startsWith('audio/') || (source && /\.(mp3|wav|ogg|m4a|flac)(?:\?|#|$)/i.test(source))
  const isPdf = mime === 'application/pdf' || (source && /\.pdf(?:\?|#|$)/i.test(source))
  const isTextual = mime.startsWith('text/') || mime.includes('json') || mime.includes('xml') || mime.includes('yaml')

  let inlineText = null
  if (isTextual && typeof artifact.url_or_base64 === 'string'
      && !/^data:|^https?:\/\//i.test(artifact.url_or_base64)
      && !artifact.url_or_base64.startsWith('/')) {
    inlineText = artifact.url_or_base64
  }

  return (
    <div className="result-media-card">
      <div className="result-media-card__meta">
        <span className="result-media-card__name">{name}</span>
        <span className="result-media-card__row">
          {mime && <span className="result-media-card__mime">{mime}</span>}
          {sizeLabel && <span className="result-media-card__size">{sizeLabel}</span>}
        </span>
      </div>
      {source && isImage && (
        <a href={source} target="_blank" rel="noopener noreferrer">
          <img className="result-media__image" src={source} alt={name} loading="lazy" />
        </a>
      )}
      {source && isVideo && (
        <video className="result-media__video" src={source} controls playsInline preload="metadata" />
      )}
      {source && isAudio && (
        <audio className="result-media__audio" src={source} controls preload="metadata" />
      )}
      {source && isPdf && (
        <div className="result-media__pdf">
          <a href={source} target="_blank" rel="noopener noreferrer" className="result-media-card__link">Open PDF ↗</a>
        </div>
      )}
      {inlineText != null && (
        <pre className="result-code-block"><code>{inlineText}</code></pre>
      )}
      {!source && inlineText == null && (
        <p className="result-media-card__fallback">Preview unavailable.</p>
      )}
      {source && !inlineText && (
        <a className="result-media-card__link" href={source} target="_blank" rel="noopener noreferrer">
          Open artifact ↗
        </a>
      )}
    </div>
  )
}

// ── Top-level component ───────────────────────────────────────────────────

export default function GenericResult({ result }) {
  const output = isPlainObject(result) ? result : { value: result }

  const summary = useMemo(() => {
    for (const key of SUMMARY_KEYS) {
      const v = output[key]
      if (typeof v === 'string' && v.trim().length > 0) return v
    }
    return null
  }, [output])

  const artifacts = useMemo(() => {
    for (const key of ARTIFACT_KEYS) {
      const v = output[key]
      if (Array.isArray(v)) return v.filter(item => item && typeof item === 'object')
    }
    return []
  }, [output])

  const detailEntries = Object.entries(output).filter(([key, value]) => {
    if (key === 'summary' && summary === value) return false
    if (ARTIFACT_KEYS.includes(key)) return false
    return true
  })

  if (detailEntries.length === 0 && !summary && artifacts.length === 0) {
    return <pre className="result-json">{JSON.stringify(result, null, 2)}</pre>
  }

  return (
    <div className="result">
      {summary && (
        <div className="result-box result-box--accent">
          <p className="result-summary"><LinkedText text={summary} /></p>
        </div>
      )}

      {artifacts.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Artifacts</p>
          <div className="result-media-grid">
            {artifacts.map((artifact, index) => (
              <ArtifactCard key={`${artifact.name ?? 'artifact'}-${index}`} artifact={artifact} />
            ))}
          </div>
        </div>
      )}

      {detailEntries.length > 0 && (
        <div className="result-section">
          <p className="result-section__label">Details</p>
          <div className="result-generic-details">
            {detailEntries.map(([key, value]) => (
              <div key={key} className="result-generic-row">
                <p className="result-generic-row__key">{titleize(key)}</p>
                {isPrimitive(value) ? (
                  <PrimitiveValue value={value} hintKey={key} />
                ) : Array.isArray(value) ? (
                  <ArrayValue items={value} />
                ) : (
                  <KeyValueBlock value={value} compact />
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
