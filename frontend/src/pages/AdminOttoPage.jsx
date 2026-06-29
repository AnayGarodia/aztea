// Otto product-telemetry dashboard (admin only). One screen, ten tabs, every
// metric the app reports — growth, demand, quality, latency, cost, reliability,
// setup, learning — plus the intent×app matrix that ranks what to fix next.
//
// Data comes from GET /admin/otto/metrics (all sections in one initial load).
// Reliable quality and latency are the two determining factors for a computer-
// use agent, so those tabs are the core; everything else frames them.

import { useEffect, useMemo, useState } from 'react'
import Topbar from '../layout/Topbar'
import Skeleton from '../ui/Skeleton'
import Card from '../ui/Card'
import { useMarket } from '../context/MarketContext'
import { fetchOttoMetrics } from '../api'
import {
  Bars,
  CHART_COLORS,
  Empty,
  Funnel,
  Kpi,
  LineSeries,
  Panel,
  Table,
  fmtMsShort,
  fmtNum,
  fmtPct,
} from '../components/otto/widgets'
import './AdminOttoPage.css'

const WINDOWS = ['7d', '30d', '90d']
const TABS = [
  ['overview', 'Overview'],
  ['growth', 'Growth & Retention'],
  ['usage', 'Usage & Demand'],
  ['quality', 'Quality'],
  ['latency', 'Latency'],
  ['matrix', 'The Matrix'],
  ['cost', 'Cost & Margin'],
  ['reliability', 'Reliability'],
  ['setup', 'Setup & Onboarding'],
  ['learning', 'Learning'],
]

export default function AdminOttoPage() {
  const { apiKey } = useMarket()
  const [windowSel, setWindowSel] = useState('30d')
  const [tab, setTab] = useState('overview')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let live = true
    setLoading(true)
    setError(null)
    fetchOttoMetrics(apiKey, { window: windowSel })
      .then((body) => { if (live) setData(body.sections || {}) })
      .catch((e) => { if (live) setError(e?.message ?? 'Failed to load Otto metrics.') })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [apiKey, windowSel])

  const section = data?.[tab]

  return (
    <main className="otto-dash">
      <Topbar crumbs={[{ label: 'Otto Telemetry' }]} />
      <div className="otto-dash__scroll">
        <div className="otto-dash__content">
          <header className="otto-dash__header">
            <div>
              <p className="otto-dash__eyebrow t-micro">Admin only</p>
              <h1>Otto product telemetry</h1>
              <p>What users do, whether it works, how fast it feels, and what it costs.</p>
            </div>
            <div className="otto-dash__windows">
              {WINDOWS.map((w) => (
                <button
                  key={w}
                  className={`otto-chip ${w === windowSel ? 'otto-chip--on' : ''}`}
                  onClick={() => setWindowSel(w)}
                >{w}</button>
              ))}
            </div>
          </header>

          <nav className="otto-tabs">
            {TABS.map(([key, label]) => (
              <button
                key={key}
                className={`otto-tab ${key === tab ? 'otto-tab--on' : ''}`}
                onClick={() => setTab(key)}
              >{label}</button>
            ))}
          </nav>

          {error ? (
            <Card><div className="otto-error">{error}</div></Card>
          ) : loading || !data ? (
            <div className="otto-grid"><Skeleton style={{ height: 120 }} /><Skeleton style={{ height: 120 }} /><Skeleton style={{ height: 120 }} /></div>
          ) : !section ? (
            <Empty />
          ) : (
            <SectionView tab={tab} d={section} window={windowSel} />
          )}
        </div>
      </div>
    </main>
  )
}

function SectionView({ tab, d, window }) {
  switch (tab) {
    case 'overview': return <Overview d={d} />
    case 'growth': return <Growth d={d} />
    case 'usage': return <Usage d={d} />
    case 'quality': return <Quality d={d} />
    case 'latency': return <Latency d={d} window={window} />
    case 'matrix': return <Matrix d={d} />
    case 'cost': return <Cost d={d} />
    case 'reliability': return <Reliability d={d} />
    case 'setup': return <Setup d={d} />
    case 'learning': return <Learning d={d} />
    default: return <Empty />
  }
}

// ── Overview ────────────────────────────────────────────────────────────────

function Overview({ d }) {
  return (
    <div className="otto-kpis">
      <Kpi label="Downloads" value={d.downloads?.value} trend={d.downloads} hint="Website button clicks" />
      <Kpi label="Installs" value={d.installs?.value} trend={d.installs} hint="First app launch" />
      <Kpi label="Active devices" value={d.active_devices?.value} trend={d.active_devices} />
      <Kpi label="Tasks" value={d.tasks} />
      <Kpi label="Success rate" value={d.success_rate} format="pct" hint="Tasks that finished right" />
      <Kpi label="P95 task time" value={d.p95_total_ms} format="ms" hint="Slow-5% wall clock" />
      <Kpi label="P95 time to first move" value={d.p95_ttfa_ms} format="ms" hint="‘Is it frozen?’ number" />
      <Kpi label="Cost / task" value={d.cost_per_task_usd != null ? `$${d.cost_per_task_usd}` : '—'} />
    </div>
  )
}

// ── Growth & Retention ──────────────────────────────────────────────────────

function Growth({ d }) {
  const f = d.funnel || {}
  return (
    <div className="otto-grid">
      <Panel title="Funnel" question="Where do we lose people from click to retained?">
        <Funnel stages={[
          { label: 'Downloads', value: f.downloads || 0 },
          { label: 'Installs', value: f.installs || 0 },
          { label: 'First success', value: f.activated || 0 },
          { label: 'Retained (2+ days)', value: f.retained || 0 },
        ]} />
      </Panel>
      <Panel title="Active devices per day" question="Is the daily-active base growing?" wide>
        <LineSeries data={d.active_timeseries} xKey="day" lines={[{ key: 'devices', label: 'Active devices' }]} />
      </Panel>
      <Panel title="Retention cohorts" question="Of each install-day cohort, how many came back?" wide>
        <Table
          empty="No install cohorts yet."
          columns={[
            { key: 'cohort_day', label: 'Install day' },
            { key: 'size', label: 'Cohort', align: 'right', format: fmtNum },
            { key: 'd1_pct', label: 'Day 1', align: 'right', format: fmtPct, heat: (v) => v },
            { key: 'd7_pct', label: 'Week 1', align: 'right', format: fmtPct, heat: (v) => v },
          ]}
          rows={d.retention_cohorts}
        />
      </Panel>
    </div>
  )
}

// ── Usage & Demand ──────────────────────────────────────────────────────────

function Usage({ d }) {
  const summon = d.summon || {}
  return (
    <div className="otto-grid">
      <Panel title="Tasks per day" question="How much is Otto used?" wide>
        <LineSeries data={d.tasks_timeseries} xKey="day" lines={[{ key: 'tasks', label: 'Tasks' }]} />
      </Panel>
      <Panel title="Top task types" question="What do people ask Otto to do?">
        <Bars data={d.top_intents} xKey="intent" yKey="n" />
      </Panel>
      <Panel title="Top apps" question="Where is Otto pointed?">
        <Bars data={d.top_apps} xKey="app" yKey="n" color="var(--accent-2)" />
      </Panel>
      <Panel title="Unmet demand" question="What did users ask that Otto couldn’t do? (the roadmap)" wide>
        <Table
          empty="Nothing unmet — or not enough data yet."
          columns={[
            { key: 'intent', label: 'Intent' },
            { key: 'app', label: 'App' },
            { key: 'n', label: 'Times asked', align: 'right', format: fmtNum },
          ]}
          rows={d.unmet_demand}
        />
      </Panel>
      <Panel title="Voice vs typed" question="How do people summon Otto?">
        <Bars
          data={[{ k: 'Voice', n: summon.voice || 0 }, { k: 'Typed', n: summon.typed || 0 }]}
          xKey="k" yKey="n"
        />
      </Panel>
    </div>
  )
}

// ── Quality ─────────────────────────────────────────────────────────────────

function Quality({ d }) {
  const t = d.totals || {}
  return (
    <div className="otto-grid">
      <Panel title="Outcomes" question="Of all tasks, how many finished right?">
        <div className="otto-kpis">
          <Kpi label="Success rate" value={t.success_rate} format="pct" />
          <Kpi label="Success" value={t.success} />
          <Kpi label="Partial" value={t.partial} />
          <Kpi label="Failed" value={t.failed} />
          <Kpi label="Stopped" value={t.stopped} />
        </div>
      </Panel>
      <Panel title="Why it fails" question="What broke? (ranked — your bug list)">
        <Bars data={d.failure_reasons} xKey="reason" yKey="n" color="var(--negative)" />
      </Panel>
      <Panel title="Success by task type" question="Where is Otto good vs bad?" wide>
        <Table
          columns={[
            { key: 'intent', label: 'Intent' },
            { key: 'n', label: 'Tasks', align: 'right', format: fmtNum },
            { key: 'success_rate', label: 'Success', align: 'right', format: fmtPct, heat: (v) => v },
          ]}
          rows={d.success_by_intent}
        />
      </Panel>
    </div>
  )
}

// ── Latency ─────────────────────────────────────────────────────────────────

function Latency({ d, window }) {
  const h = d.headline || {}
  const comp = d.components || {}
  const path = d.path || {}
  const compRows = [
    { stage: 'Perceive', ...comp.perceive_ms },
    { stage: 'Think (model)', ...comp.model_ms },
    { stage: 'Act', ...comp.act_ms },
    { stage: 'Verify', ...comp.verify_ms },
  ]
  return (
    <div className="otto-grid">
      <Panel title="How fast it feels" question="Time to first move and total time (P50 / P95 / P99).">
        <div className="otto-kpis">
          <Kpi label="First move P50" value={h.ttfa_ms?.p50} format="ms" />
          <Kpi label="First move P95" value={h.ttfa_ms?.p95} format="ms" />
          <Kpi label="Total P50" value={h.total_ms?.p50} format="ms" />
          <Kpi label="Total P95" value={h.total_ms?.p95} format="ms" />
          <Kpi label="Total P99" value={h.total_ms?.p99} format="ms" />
        </div>
      </Panel>
      <Panel title={`P95 total over time (${window})`} question="Is Otto getting slower?" wide>
        <LineSeries
          data={d.p95_timeseries}
          xKey="day"
          lines={[{ key: 'p95', label: 'P95', color: 'var(--negative)' }, { key: 'p50', label: 'P50' }]}
        />
      </Panel>
      <Panel title="Where the time goes" question="Which stage of the loop costs the most?">
        <Table
          columns={[
            { key: 'stage', label: 'Stage' },
            { key: 'p50', label: 'P50', align: 'right', format: fmtMsShort },
            { key: 'p95', label: 'P95', align: 'right', format: fmtMsShort },
          ]}
          rows={compRows}
        />
      </Panel>
      <Panel title="Fast path vs slow path" question="How often is Otto forced onto slow vision reads?">
        <div className="otto-kpis">
          <Kpi label="Vision (slow) share" value={path.vision_share} format="pct" hint="Lower is better" />
          <Kpi label="Fast (structured) share" value={path.fast_share} format="pct" />
          <Kpi label="Vision steps" value={path.vision_steps} />
          <Kpi label="Total steps" value={path.total_steps} />
        </div>
      </Panel>
      <Panel title="Speed by model" question="Which model is fastest at equal work?" wide>
        <Table
          empty="No per-model timing yet."
          columns={[
            { key: 'model', label: 'Model' },
            { key: 'calls', label: 'Calls', align: 'right', format: fmtNum },
            { key: 'avg_ms', label: 'Avg latency', align: 'right', format: fmtMsShort },
          ]}
          rows={d.by_model}
        />
      </Panel>
    </div>
  )
}

// ── The Matrix ──────────────────────────────────────────────────────────────

function Matrix({ d }) {
  return (
    <Panel
      title="Intent × App — what to fix next"
      question="Each task type on each app: success, worst-case time, slow-path share, cost. Sorted by volume, so the worst high-frequency row is the top priority."
      wide
    >
      <Table
        empty="No tasks in this window yet."
        columns={[
          { key: 'intent', label: 'Intent' },
          { key: 'app', label: 'App' },
          { key: 'tasks', label: 'Tasks', align: 'right', format: fmtNum },
          { key: 'success_rate', label: 'Success', align: 'right', format: fmtPct, heat: (v) => v },
          { key: 'p95_total_ms', label: 'P95 time', align: 'right', format: fmtMsShort },
          { key: 'vision_share', label: 'Slow path', align: 'right', format: fmtPct },
          { key: 'cost_per_task_usd', label: '$/task', align: 'right', format: (v) => (v == null ? '—' : `$${v}`) },
        ]}
        rows={d.rows}
      />
    </Panel>
  )
}

// ── Cost & Margin ───────────────────────────────────────────────────────────

function Cost({ d }) {
  return (
    <div className="otto-grid">
      <Panel title="Cost summary" question="Does the unit economics work?">
        <div className="otto-kpis">
          <Kpi label="Total cost" value={d.total_cost_usd != null ? `$${d.total_cost_usd}` : '—'} />
          <Kpi label="Cost / task" value={d.cost_per_task_usd != null ? `$${d.cost_per_task_usd}` : '—'} />
        </div>
      </Panel>
      <Panel title="Cost over time" question="Is spend tracking usage?" wide>
        <LineSeries data={d.cost_timeseries} xKey="day" lines={[{ key: 'cost_usd', label: 'Cost (USD)' }]} />
      </Panel>
      <Panel title="Cost by task type" question="Which jobs are expensive?">
        <Bars data={d.cost_by_intent} xKey="intent" yKey="cost" color="var(--warn)" />
      </Panel>
      <Panel title="Heaviest devices" question="Who could a flat price make unprofitable?" wide>
        <Table
          empty="No device cost data yet."
          columns={[
            { key: 'device_id', label: 'Device (anon)' },
            { key: 'n', label: 'Tasks', align: 'right', format: fmtNum },
            { key: 'cost_usd', label: 'Cost', align: 'right', format: (v) => `$${v}` },
          ]}
          rows={(d.top_cost_devices || []).map((r) => ({ ...r, device_id: String(r.device_id || '').slice(0, 8) }))}
        />
      </Panel>
    </div>
  )
}

// ── Reliability ─────────────────────────────────────────────────────────────

function Reliability({ d }) {
  return (
    <div className="otto-grid">
      <Panel title="Reliability" question="App-level failures, separate from task outcomes.">
        <div className="otto-kpis">
          <Kpi label="Total errors" value={d.total_errors} />
          <Kpi label="Errors / active device" value={d.errors_per_active_device} />
        </div>
      </Panel>
      <Panel title="Errors by kind" question="Crashes, hangs, timeouts, voice glitches — what dominates?" wide>
        <Bars data={d.errors_by_kind} xKey="kind" yKey="n" color="var(--negative)" />
      </Panel>
    </div>
  )
}

// ── Setup & Onboarding ──────────────────────────────────────────────────────

function Setup({ d }) {
  return (
    <div className="otto-grid">
      <Panel title="Permission grant rate" question="The silent killer — who quits at the macOS permission gate?" wide>
        <Table
          empty="No permission events yet."
          columns={[
            { key: 'kind', label: 'Permission' },
            { key: 'granted', label: 'Granted', align: 'right', format: fmtNum },
            { key: 'total', label: 'Prompted', align: 'right', format: fmtNum },
            { key: 'grant_rate', label: 'Grant rate', align: 'right', format: fmtPct, heat: (v) => v },
          ]}
          rows={d.permission_grant}
        />
      </Panel>
      <Panel title="Onboarding funnel" question="Which setup step do people abandon?" wide>
        <Table
          empty="No onboarding events yet."
          columns={[
            { key: 'step', label: 'Step' },
            { key: 'reached', label: 'Reached', align: 'right', format: fmtNum },
            { key: 'completed', label: 'Completed', align: 'right', format: fmtNum },
            { key: 'abandoned', label: 'Abandoned', align: 'right', format: fmtNum },
          ]}
          rows={d.onboarding}
        />
      </Panel>
      <Panel title="Accounts connected" question="More connectors = stickier.">
        <Bars data={d.accounts_connected} xKey="provider" yKey="n" />
      </Panel>
    </div>
  )
}

// ── Learning ────────────────────────────────────────────────────────────────

function Learning({ d }) {
  const r = d.repeat || {}
  const f = d.first_time || {}
  return (
    <div className="otto-grid">
      <Panel title="Does Otto get smarter on repeats?" question="Repeat tasks should run from memory — faster and cheaper.">
        <div className="otto-kpis">
          <Kpi label="Repeat share" value={d.repeat_share} format="pct" />
          <Kpi label="Repeat avg time" value={r.avg_total_ms} format="ms" />
          <Kpi label="First-time avg time" value={f.avg_total_ms} format="ms" />
          <Kpi label="Repeat avg cost" value={r.avg_cost_usd != null ? `$${r.avg_cost_usd}` : '—'} />
          <Kpi label="First-time avg cost" value={f.avg_cost_usd != null ? `$${f.avg_cost_usd}` : '—'} />
        </div>
      </Panel>
    </div>
  )
}
