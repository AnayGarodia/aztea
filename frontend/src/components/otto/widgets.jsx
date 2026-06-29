// Presentational building blocks for the Otto telemetry dashboard.
// All colours come from theme tokens (never hardcoded) so the charts track
// light/dark. Each widget is dumb: it takes already-computed numbers from the
// /admin/otto/metrics API and renders. No data fetching here.

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

const AXIS = 'var(--text-muted)'
const GRID = 'var(--border-soft)'
const ACCENT = 'var(--accent)'

const TOOLTIP_STYLE = {
  background: 'var(--surface)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--r-sm)',
  color: 'var(--text-primary)',
  fontSize: 12,
}

function fmtNum(n) {
  if (n == null) return '—'
  if (typeof n !== 'number') return String(n)
  if (Math.abs(n) >= 1000) return n.toLocaleString()
  return String(n)
}

function fmtMsShort(ms) {
  if (ms == null) return '—'
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`
  return `${Math.round(ms)}ms`
}

function fmtPct(rate) {
  if (rate == null) return '—'
  return `${Math.round(rate * 100)}%`
}

// ── KPI card with trend delta vs the prior period ───────────────────────────

export function Kpi({ label, value, trend, format = 'num', hint }) {
  const fmt = format === 'ms' ? fmtMsShort : format === 'pct' ? fmtPct : fmtNum
  const delta = trend?.delta_pct
  const dir = delta == null ? 'flat' : delta > 0 ? 'up' : delta < 0 ? 'down' : 'flat'
  return (
    <div className="otto-kpi">
      <div className="otto-kpi__label">{label}</div>
      <div className="otto-kpi__value">{fmt(value)}</div>
      {trend != null && (
        <div className={`otto-kpi__delta otto-kpi__delta--${dir}`}>
          {delta == null ? 'no prior data' : `${delta > 0 ? '▲' : delta < 0 ? '▼' : '■'} ${Math.abs(delta)}% vs prior`}
        </div>
      )}
      {hint && <div className="otto-kpi__hint">{hint}</div>}
    </div>
  )
}

// ── Section wrapper: plain-English title + the question it answers ───────────

export function Panel({ title, question, children, wide = false }) {
  return (
    <section className={`otto-panel ${wide ? 'otto-panel--wide' : ''}`}>
      <header className="otto-panel__head">
        <h3>{title}</h3>
        {question && <p>{question}</p>}
      </header>
      <div className="otto-panel__body">{children}</div>
    </section>
  )
}

export function Empty({ children = 'No data in this window yet.' }) {
  return <div className="otto-empty">{children}</div>
}

// ── Time-series line (e.g. daily active devices, P95 latency over time) ──────

export function LineSeries({ data, xKey, lines, height = 220 }) {
  if (!data?.length) return <Empty />
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey={xKey} stroke={AXIS} tick={{ fontSize: 11 }} minTickGap={24} />
        <YAxis stroke={AXIS} tick={{ fontSize: 11 }} width={40} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        {lines.map((l) => (
          <Line
            key={l.key}
            type="monotone"
            dataKey={l.key}
            name={l.label || l.key}
            stroke={l.color || ACCENT}
            strokeWidth={2}
            dot={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}

// ── Horizontal-ish bar (top intents, top apps, failure reasons) ─────────────

export function Bars({ data, xKey, yKey, height = 240, color = ACCENT }) {
  if (!data?.length) return <Empty />
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid stroke={GRID} vertical={false} />
        <XAxis dataKey={xKey} stroke={AXIS} tick={{ fontSize: 11 }} interval={0} angle={-20} textAnchor="end" height={56} />
        <YAxis stroke={AXIS} tick={{ fontSize: 11 }} width={40} />
        <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: 'var(--accent-bg)' }} />
        <Bar dataKey={yKey} fill={color} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  )
}

// ── Funnel: ordered stages with a shrinking bar + conversion vs first stage ──

export function Funnel({ stages }) {
  const top = stages?.[0]?.value || 0
  if (!top) return <Empty />
  return (
    <div className="otto-funnel">
      {stages.map((s) => {
        const pct = top ? Math.round((s.value / top) * 100) : 0
        return (
          <div className="otto-funnel__row" key={s.label}>
            <div className="otto-funnel__meta">
              <span>{s.label}</span>
              <span>{fmtNum(s.value)} · {pct}%</span>
            </div>
            <div className="otto-funnel__track">
              <div className="otto-funnel__fill" style={{ width: `${pct}%` }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── Generic data table with optional cell formatters + heat colouring ───────

export function Table({ columns, rows, empty }) {
  if (!rows?.length) return <Empty>{empty || 'No rows in this window yet.'}</Empty>
  return (
    <div className="otto-table-wrap">
      <table className="otto-table">
        <thead>
          <tr>{columns.map((c) => <th key={c.key} style={c.align ? { textAlign: c.align } : null}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r._key || i}>
              {columns.map((c) => {
                const raw = r[c.key]
                const val = c.format ? c.format(raw, r) : (raw ?? '—')
                return (
                  <td key={c.key} style={{ textAlign: c.align || 'left', ...(c.heat ? heatStyle(c.heat(raw, r)) : null) }}>
                    {val}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// heat: 0 (bad) → 1 (good), tinted from negative to positive token colours.
function heatStyle(t) {
  if (t == null) return null
  const clamped = Math.max(0, Math.min(1, t))
  // mix using rgba of positive/negative tokens via CSS color-mix when available.
  return { background: `color-mix(in srgb, var(--positive-bg) ${Math.round(clamped * 100)}%, var(--negative-bg))` }
}

export { fmtNum, fmtMsShort, fmtPct }
export const CHART_COLORS = ['var(--accent)', 'var(--accent-2)', 'var(--sage)', 'var(--warn)', 'var(--border-bright)']
