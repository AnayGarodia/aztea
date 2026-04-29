import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import { fetchAgents, fetchWalletMe, fetchRuns, fetchJobs } from '../api'

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
    if (code === 'network.timeout' || code === 'network.error') return
    const msg = (err && err.message) ? err.message : fallbackMsg
    if (lastRefreshError.current !== msg) {
      showToast(msg, 'error')
      lastRefreshError.current = msg
    }
  }, [showToast])

  const refresh = useCallback(async () => {
    try {
      const [ag, wl, ru, jb] = await Promise.all([
        fetchAgents(apiKey),
        fetchWalletMe(apiKey),
        fetchRuns(apiKey),
        fetchJobs(apiKey, { limit: 50 }),
      ])
      setAgents(ag.agents ?? [])
      setWallet(wl)
      setRuns(ru.runs ?? [])
      setJobs(jb.jobs ?? [])
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh dashboard data.')
    }
  }, [apiKey, reportRefreshError])

  // Background poll: only refresh wallet + recent jobs (not full agent list)
  const backgroundPoll = useCallback(async () => {
    try {
      const [wl, jb] = await Promise.all([
        fetchWalletMe(apiKey),
        fetchJobs(apiKey, { limit: 50 }),
      ])
      setWallet(wl)
      setJobs(prev => {
        const incoming = jb.jobs ?? []
        // Merge: update existing rows, prepend truly new ones
        const existingIds = new Set(prev.map(j => j.job_id))
        const updated = prev.map(j => incoming.find(i => i.job_id === j.job_id) ?? j)
        const newOnes = incoming.filter(j => !existingIds.has(j.job_id))
        return [...newOnes, ...updated]
      })
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh dashboard data.')
    }
  }, [apiKey, reportRefreshError])

  const refreshWallet = useCallback(async () => {
    try {
      setWallet(await fetchWalletMe(apiKey))
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh wallet.')
    }
  }, [apiKey, reportRefreshError])

  const refreshJobs = useCallback(async () => {
    try {
      const jb = await fetchJobs(apiKey, { limit: 50 })
      setJobs(jb.jobs ?? [])
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh jobs.')
    }
  }, [apiKey, reportRefreshError])

  useEffect(() => {
    refresh().finally(() => setLoading(false))
    const id = setInterval(backgroundPoll, 20000)
    return () => clearInterval(id)
  }, [refresh, backgroundPoll])

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
