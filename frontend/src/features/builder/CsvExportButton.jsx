// OWNS: client-side CSV export of per-agent earnings.
// NOT OWNS: server-side export endpoint (we deliberately do this client-
//           side to avoid a PII export-log compliance concern). Earnings
//           data already lives in MarketContext for the authed user.
// DECISIONS:
//   * Excel/Sheets-friendly: comma-delimited, RFC 4180 quoting, BOM-less
//     UTF-8. Sheets opens this cleanly via File → Import.
//   * Filename includes the ISO date so an export taken on different days
//     doesn't overwrite.
//   * No external CSV library — three reasons:
//       (1) keeps the bundle small,
//       (2) the column set is fixed and small,
//       (3) we control quoting end-to-end.

import Button from '../../ui/Button'
import { Download } from 'lucide-react'

const CSV_HEADERS = [
  'agent_id',
  'agent_name',
  'status',
  'price_per_call_usd',
  'total_earned_usd',
  'call_count',
  'success_rate',
  'median_latency_seconds',
]

function _csvCell(value) {
  if (value === null || value === undefined) return ''
  const s = String(value)
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"'
  }
  return s
}

function _rowsToCsv(rows) {
  const lines = [CSV_HEADERS.join(',')]
  for (const r of rows) {
    lines.push(CSV_HEADERS.map(h => _csvCell(r[h])).join(','))
  }
  return lines.join('\n') + '\n'
}

function _todayIso() {
  return new Date().toISOString().slice(0, 10) // YYYY-MM-DD
}

function _triggerDownload(filename, content) {
  const blob = new Blob([content], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

export default function CsvExportButton({ agents, earningsMap, disabled }) {
  const rowCount = agents?.length ?? 0
  const handleExport = () => {
    const rows = agents.map(agent => {
      const earnings = earningsMap[agent.agent_id] ?? {}
      const totalCents = earnings.total_earned_cents ?? 0
      return {
        agent_id: agent.agent_id,
        agent_name: agent.name ?? '',
        status: agent.status ?? 'active',
        price_per_call_usd: (agent.price_per_call_usd ?? 0).toFixed(4),
        total_earned_usd: (totalCents / 100).toFixed(2),
        call_count: earnings.call_count ?? 0,
        success_rate:
          earnings.success_rate != null
            ? Number(earnings.success_rate).toFixed(3)
            : '',
        median_latency_seconds:
          earnings.median_latency_seconds != null
            ? Number(earnings.median_latency_seconds).toFixed(2)
            : '',
      }
    })
    _triggerDownload(`aztea-agents-${_todayIso()}.csv`, _rowsToCsv(rows))
  }

  return (
    <Button
      variant="ghost"
      size="sm"
      icon={<Download size={14} />}
      onClick={handleExport}
      disabled={disabled || rowCount === 0}
      title={
        rowCount === 0
          ? 'No agents to export'
          : `Export ${rowCount} agent${rowCount === 1 ? '' : 's'} as CSV`
      }
    >
      Export CSV
    </Button>
  )
}
