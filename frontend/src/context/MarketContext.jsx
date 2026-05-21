// OWNS: global state — agents list, wallet balance, jobs, runs
// NOT OWNS: auth/session state (AuthContext), individual job detail (fetched per-page)
//
// INVARIANTS:
// - wallet balance here is for display only; always re-fetch before charging
// - Phoenix WebSocket → SSE → poll is the cascade. Removing any link must keep
//   the others functional; the WebSocket is additive and never load-bearing.
//
// DECISIONS:
// - jobs + runs fetched on the same poll tick to keep sidebar badges in sync;
//   splitting to different intervals caused visible count inconsistency
// - SSE fan-out in _record_job_event covers both caller and agent owner IDs so
//   workers see their job queue update in real time too
// - useRef for interval id (not useState) to avoid re-renders on every tick
// - Phoenix socket is best-effort: if /auth/socket-token returns 503 (sidecar
//   not configured) or the connect fails, we keep the original 5 s poll. When
//   the socket IS connected we slow the poll to 60 s — it stays on as a
//   reconciliation net but no longer needs to be the primary update path.

import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import { fetchAgents, fetchWalletMe, fetchRuns, fetchJobs } from '../api'
import { RealtimeSession } from '../lib/realtime'

const Ctx = createContext(null)

export function MarketProvider({ apiKey, children }) {
  const [agents, setAgents]     = useState([])
  const [wallet, setWallet]     = useState(null)
  const [runs,   setRuns]       = useState([])
  const [jobs,   setJobs]       = useState([])
  const [loading, setLoading]   = useState(true)
  const [toast,   setToast]     = useState(null)
  const toastTimer              = useRef(null)
  const lastRefreshError        = useRef('')

  const showToast = useCallback((msg, type = 'info') => {
    clearTimeout(toastTimer.current)
    setToast({ msg, type, id: Date.now() })
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  const reportRefreshError = useCallback((err, fallbackMsg) => {
    // Background polls fail constantly during transient network blips. Don't
    // toast for timeouts/network errors — the next successful poll silently
    // recovers and surfacing them just spams the corner.
    const code = err?.code
    if (code === 'network.timeout' || code === 'network.error' || err?.authInvalid) return
    const msg = (err && err.message) ? err.message : fallbackMsg
    if (lastRefreshError.current !== msg) {
      showToast(msg, 'error')
      lastRefreshError.current = msg
    }
  }, [showToast])

  const refresh = useCallback(async () => {
    if (!apiKey) {
      try {
        const ag = await fetchAgents(null)
        setAgents(ag.agents ?? [])
        lastRefreshError.current = ''
      } catch (err) {
        reportRefreshError(err, 'Failed to refresh catalog.')
      }
      return
    }
    // allSettled so a single failing endpoint (e.g. wallet) doesn't blank the others.
    const [agR, wlR, ruR, jbR] = await Promise.allSettled([
      fetchAgents(apiKey),
      fetchWalletMe(apiKey),
      fetchRuns(apiKey),
      fetchJobs(apiKey, { limit: 50 }),
    ])
    if (agR.status === 'fulfilled') setAgents(agR.value.agents ?? [])
    if (wlR.status === 'fulfilled') setWallet(wlR.value)
    if (ruR.status === 'fulfilled') setRuns(ruR.value.runs ?? [])
    if (jbR.status === 'fulfilled') setJobs(jbR.value.jobs ?? [])
    const firstError = [agR, wlR, ruR, jbR].find(r => r.status === 'rejected')
    if (firstError) {
      reportRefreshError(firstError.reason, 'Failed to refresh dashboard data.')
    } else {
      lastRefreshError.current = ''
    }
  }, [apiKey, reportRefreshError])

  // Background poll: only refresh wallet + recent jobs (not full agent list)
  const backgroundPoll = useCallback(async () => {
    if (!apiKey) return
    const [wlR, jbR] = await Promise.allSettled([
      fetchWalletMe(apiKey),
      fetchJobs(apiKey, { limit: 50 }),
    ])
    if (wlR.status === 'fulfilled') setWallet(wlR.value)
    if (jbR.status === 'fulfilled') {
      setJobs(prev => {
        const incoming = jbR.value.jobs ?? []
        // Merge: update existing rows, prepend truly new ones
        const existingIds = new Set(prev.map(j => j.job_id))
        const updated = prev.map(j => incoming.find(i => i.job_id === j.job_id) ?? j)
        const newOnes = incoming.filter(j => !existingIds.has(j.job_id))
        return [...newOnes, ...updated]
      })
    }
    const firstError = [wlR, jbR].find(r => r.status === 'rejected')
    if (firstError) {
      reportRefreshError(firstError.reason, 'Failed to refresh dashboard data.')
    } else {
      lastRefreshError.current = ''
    }
  }, [apiKey, reportRefreshError])

  const refreshWallet = useCallback(async () => {
    if (!apiKey) return
    try {
      setWallet(await fetchWalletMe(apiKey))
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh wallet.')
    }
  }, [apiKey, reportRefreshError])

  const refreshJobs = useCallback(async () => {
    if (!apiKey) return
    try {
      const jb = await fetchJobs(apiKey, { limit: 50 })
      setJobs(jb.jobs ?? [])
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh jobs.')
    }
  }, [apiKey, reportRefreshError])

  // Merge a single job event into the jobs list without a full refetch.
  const applyJobEvent = useCallback((event) => {
    if (!apiKey) return
    const jobId = event?.job_id
    if (!jobId) return
    setJobs(prev => {
      const idx = prev.findIndex(j => j.job_id === jobId)
      if (idx === -1) {
        // New job not yet in list — trigger a reconciliation fetch.
        fetchJobs(apiKey, { limit: 50 }).then(r => setJobs(r.jobs ?? [])).catch(() => {})
        return prev
      }
      const updated = [...prev]
      updated[idx] = { ...updated[idx], status: event.event_type?.replace('job.', '') ?? updated[idx].status }
      return updated
    })
    // Wallet balance may have changed (charge or refund settled); re-fetch it.
    fetchWalletMe(apiKey).then(setWallet).catch(() => {})
  }, [apiKey])

  // Open SSE connection for real-time job updates; fall back to 60s poll.
  useEffect(() => {
    if (!apiKey) return
    const url = `/jobs/events?key=${encodeURIComponent(apiKey)}`
    let es = null
    let retryTimer = null
    let closed = false
    let failCount = 0

    const connect = () => {
      if (closed) return
      es = new EventSource(url)
      es.onmessage = (e) => {
        failCount = 0
        try { applyJobEvent(JSON.parse(e.data)) } catch (_) {}
      }
      es.onerror = () => {
        es.close()
        if (closed) return
        failCount++
        // After 3 consecutive failures give up — 60s poll keeps data current.
        if (failCount >= 3) return
        // Exponential backoff: 5s, 15s, 45s (capped at 60s).
        const delay = Math.min(5000 * Math.pow(3, failCount - 1), 60000)
        retryTimer = setTimeout(connect, delay)
      }
    }

    // Probe auth before opening a persistent SSE stream to avoid silent 4xx retry loops.
    // EventSource can't expose HTTP status codes, so a pre-flight fetch detects 401/403.
    fetch(url, { method: 'HEAD' }).then(r => {
      if (closed) return
      if (r.ok || r.status === 405) connect()
      // 401/403 → auth will fail on SSE too; 60s poll is the fallback.
    }).catch(() => {
      if (!closed) connect()
    })

    return () => {
      closed = true
      clearTimeout(retryTimer)
      if (es) es.close()
    }
  }, [apiKey, applyJobEvent])

  useEffect(() => {
    refresh().finally(() => setLoading(false))

    if (!apiKey) return undefined

    // Two-tier polling cadence. Phoenix WebSocket (if it connects) pushes
    // sub-second updates, so we slow the poll to 60s. Otherwise we stay
    // on the 5s SSE-fallback cadence the dashboard has always used.
    const POLL_FAST_MS = 5000
    const POLL_SLOW_MS = 60000
    let pollHandle = setInterval(backgroundPoll, POLL_FAST_MS)
    let currentInterval = POLL_FAST_MS

    const adjustPoll = (socketConnected) => {
      const next = socketConnected ? POLL_SLOW_MS : POLL_FAST_MS
      if (next === currentInterval) return
      clearInterval(pollHandle)
      pollHandle = setInterval(backgroundPoll, next)
      currentInterval = next
    }

    // Best-effort: the session never throws. If it can't reach the sidecar
    // it stays disconnected and the FE behaves identically to before this
    // change. The applyJobEvent path is reused so socket and SSE payloads
    // converge on the same merger logic.
    const realtime = new RealtimeSession({
      apiKey,
      onJobEvent: applyJobEvent,
      onStateChange: adjustPoll,
    })
    realtime.start()

    return () => {
      clearInterval(pollHandle)
      realtime.close()
    }
  }, [apiKey, refresh, backgroundPoll, applyJobEvent])

  useEffect(() => {
    return () => clearTimeout(toastTimer.current)
  }, [])

  return (
    <Ctx.Provider value={{
      apiKey, agents, wallet, runs, jobs,
      loading, toast, showToast,
      refresh, refreshWallet, refreshJobs,
    }}>
      {children}
    </Ctx.Provider>
  )
}

export const useMarket = () => useContext(Ctx)
