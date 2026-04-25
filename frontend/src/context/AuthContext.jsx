import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { authMe, setSessionExpiredHandler } from '../api'

const Ctx = createContext(null)

export function AuthProvider({ children }) {
  const [apiKey, setApiKey] = useState(() => localStorage.getItem('aztea_key') ?? '')
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem('aztea_user') ?? 'null') } catch { return null }
  })
  const [booting, setBooting] = useState(true)

  const mergeProfile = useCallback((profile, fallbackUser = null) => ({
    user_id: profile?.user_id ?? fallbackUser?.user_id,
    username: profile?.username ?? fallbackUser?.username ?? 'Agent',
    email: profile?.email ?? fallbackUser?.email ?? '',
    role: profile?.role ?? fallbackUser?.role ?? 'both',
    scopes: profile?.scopes ?? fallbackUser?.scopes ?? [],
    legal_acceptance_required: Boolean(
      profile?.legal_acceptance_required
      ?? fallbackUser?.legal_acceptance_required
      ?? false
    ),
    legal_accepted_at: profile?.legal_accepted_at ?? fallbackUser?.legal_accepted_at ?? null,
    terms_version_current: profile?.terms_version_current ?? fallbackUser?.terms_version_current ?? null,
    privacy_version_current: profile?.privacy_version_current ?? fallbackUser?.privacy_version_current ?? null,
    terms_version_accepted: profile?.terms_version_accepted ?? fallbackUser?.terms_version_accepted ?? null,
    privacy_version_accepted: profile?.privacy_version_accepted ?? fallbackUser?.privacy_version_accepted ?? null,
  }), [])

  useEffect(() => {
    let active = true
    const bootstrap = async () => {
      if (!apiKey) {
        if (active) setBooting(false)
        return
      }
      if (active) setBooting(true)
      try {
        const profile = await authMe(apiKey)
        if (!active) return
        const merged = mergeProfile(profile, user)
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
  }, [apiKey, mergeProfile]) // eslint-disable-line

  const connect = useCallback((key, userInfo) => {
    const merged = userInfo ? mergeProfile(userInfo, userInfo) : null
    localStorage.setItem('aztea_key', key)
    if (merged) localStorage.setItem('aztea_user', JSON.stringify(merged))
    setApiKey(key)
    if (merged) setUser(merged)
  }, [mergeProfile])

  const disconnect = useCallback(() => {
    localStorage.removeItem('aztea_key')
    localStorage.removeItem('aztea_user')
    setApiKey('')
    setUser(null)
  }, [])

  const refreshProfile = useCallback(async () => {
    if (!apiKey) return null
    const profile = await authMe(apiKey)
    const merged = mergeProfile(profile, user)
    localStorage.setItem('aztea_user', JSON.stringify(merged))
    setUser(merged)
    return merged
  }, [apiKey, mergeProfile])

  useEffect(() => {
    setSessionExpiredHandler(disconnect)
    return () => setSessionExpiredHandler(null)
  }, [disconnect])

  return (
    <Ctx.Provider value={{ apiKey, user, booting, connect, disconnect, refreshProfile }}>
      {children}
    </Ctx.Provider>
  )
}

export const useAuth = () => useContext(Ctx)
