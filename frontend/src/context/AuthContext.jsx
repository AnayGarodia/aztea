import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { authMe, setSessionExpiredHandler } from '../api'

const Ctx = createContext(null)

export function AuthProvider({ children }) {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('aztea_key') ?? '')
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem('aztea_user') ?? 'null') } catch { return null }
  })
  const [booting, setBooting] = useState(true)

  useEffect(() => {
    let active = true
    const bootstrap = async () => {
      if (!apiKey) {
        if (active) setBooting(false)
        return
      }
      try {
        const profile = await authMe(apiKey)
        if (!active) return
        const merged = {
          user_id: profile.user_id ?? user?.user_id,
          username: profile.username ?? user?.username ?? 'Agent',
          email: profile.email ?? user?.email ?? '',
          scopes: profile.scopes ?? user?.scopes ?? [],
        }
        localStorage.setItem('aztea_user', JSON.stringify(merged))
        setUser(merged)
      } catch {
        if (!active) return
        localStorage.removeItem('aztea_key')
        localStorage.removeItem('aztea_user')
        setApiKey('')
        setUser(null)
      } finally {
        if (active) setBooting(false)
      }
    }
    bootstrap()
    return () => { active = false }
  }, [apiKey]) // eslint-disable-line

  const connect = useCallback((key, userInfo) => {
    localStorage.setItem('aztea_key', key)
    if (userInfo) localStorage.setItem('aztea_user', JSON.stringify(userInfo))
    setApiKey(key)
    if (userInfo) setUser(userInfo)
  }, [])

  const disconnect = useCallback(() => {
    localStorage.removeItem('aztea_key')
    localStorage.removeItem('aztea_user')
    setApiKey('')
    setUser(null)
  }, [])

  useEffect(() => {
    setSessionExpiredHandler(disconnect)
    return () => setSessionExpiredHandler(null)
  }, [disconnect])

  return (
    <Ctx.Provider value={{ apiKey, user, booting, connect, disconnect }}>
      {children}
    </Ctx.Provider>
  )
}

export const useAuth = () => useContext(Ctx)
