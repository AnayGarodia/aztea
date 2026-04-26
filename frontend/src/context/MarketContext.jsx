import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import { fetchAgents, fetchWalletMe, fetchRuns, fetchAllJobs } from '../api'

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
        fetchAllJobs(apiKey, { pageSize: 100, maxPages: 10 }),
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
      const jb = await fetchAllJobs(apiKey, { pageSize: 100, maxPages: 10 })
      setJobs(jb.jobs ?? [])
      lastRefreshError.current = ''
    } catch (err) {
      reportRefreshError(err, 'Failed to refresh jobs.')
    }
  }, [apiKey, reportRefreshError])

  useEffect(() => {
    refresh().finally(() => setLoading(false))
    const id = setInterval(refresh, 20000)
    return () => clearInterval(id)
  }, [refresh])

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
