import { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react'
import { fetchAgents, fetchWalletMe, fetchRuns } from '../api'

const Ctx = createContext(null)

export function MarketProvider({ apiKey, children }) {
  const [agents, setAgents]           = useState([])
  const [wallet, setWallet]           = useState(null)
  const [runs,   setRuns]             = useState([])
  const [loading, setLoading]         = useState(true)
  const [toast, setToast]             = useState(null)
  const toastTimer                    = useRef(null)

  const showToast = useCallback((msg, type = 'info') => {
    clearTimeout(toastTimer.current)
    setToast({ msg, type, id: Date.now() })
    toastTimer.current = setTimeout(() => setToast(null), 3500)
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [ag, wl, ru] = await Promise.all([
        fetchAgents(apiKey),
        fetchWalletMe(apiKey),
        fetchRuns(apiKey),
      ])
      setAgents(ag.agents ?? [])
      setWallet(wl)
      setRuns(ru.runs ?? [])
    } catch {}
  }, [apiKey])

  const refreshWallet = useCallback(async () => {
    try { setWallet(await fetchWalletMe(apiKey)) } catch {}
  }, [apiKey])

  useEffect(() => {
    refresh().finally(() => setLoading(false))
    const id = setInterval(refresh, 8000)
    return () => clearInterval(id)
  }, [refresh])

  return (
    <Ctx.Provider value={{ apiKey, agents, wallet, runs, loading, toast, showToast, refresh, refreshWallet }}>
      {children}
    </Ctx.Provider>
  )
}

export const useMarket = () => useContext(Ctx)
