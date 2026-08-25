import { useCallback, useRef, useState, type ReactNode } from 'react'
import * as api from '../api/client'
import { AuthContext, type Session } from './auth-context'

const STORAGE_KEY = 'bookit_session'

function loadSession(): Session | null {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as Session
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(loadSession)

  // the API client's hooks are registered once but must always see the
  // current session, so they read this ref rather than the state variable
  // they'd otherwise capture from the render they were created in
  const sessionRef = useRef<Session | null>(session)
  sessionRef.current = session

  // a page can fire several requests at once; if they all 401 on an expired
  // token they share this one refresh instead of firing N identical
  // round-trips and racing each other to write the result. Refresh tokens
  // aren't single-use today (see api.refreshTokens / BOOK-78), so this is
  // about avoiding redundant work, not about protecting a one-shot token --
  // but it becomes load-bearing the moment rotation lands.
  const inFlightRefresh = useRef<Promise<string | null> | null>(null)

  const persist = useCallback((next: Session | null) => {
    setSession(next)
    if (next) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    } else {
      localStorage.removeItem(STORAGE_KEY)
    }
  }, [])

  const refreshAccessToken = useCallback(async (): Promise<string | null> => {
    if (inFlightRefresh.current) return inFlightRefresh.current

    const current = sessionRef.current
    if (!current?.refreshToken) return null

    inFlightRefresh.current = (async () => {
      try {
        const tokens = await api.refreshTokens(current.refreshToken!)
        persist({
          email: current.email,
          accessToken: tokens.access_token,
          refreshToken: tokens.refresh_token,
        })
        return tokens.access_token
      } catch {
        // the refresh token itself is expired or invalid -- nothing left to
        // recover from, so drop the session rather than leave a dead one that
        // makes the UI look logged in
        persist(null)
        return null
      } finally {
        inFlightRefresh.current = null
      }
    })()

    return inFlightRefresh.current
  }, [persist])

  // indirection so the stable hooks object below always calls the current
  // refreshAccessToken instead of freezing the first one it saw
  const refreshRef = useRef(refreshAccessToken)
  refreshRef.current = refreshAccessToken

  // Stable across renders: both accessors read refs rather than captured
  // state, so the object never needs rebuilding -- which also lets the
  // unmount cleanup below identify precisely these hooks.
  const hooksRef = useRef<api.AuthHooks | null>(null)
  hooksRef.current ??= {
    getAccessToken: () => sessionRef.current?.accessToken ?? null,
    refreshAccessToken: () => refreshRef.current(),
  }

  // Registered during render, NOT in an effect. React runs child effects before
  // parent ones, so a page whose first fetch happens in a child's useEffect
  // (Feed does exactly this) would fire before the provider's effect had told
  // the client about the session -- that request would silently go out with no
  // Authorization header on every full page load. Assigning module state is
  // idempotent, so repeating it per render is harmless.
  api.configureAuth(hooksRef.current)

  // Deliberately no unmount cleanup. Clearing on unmount breaks StrictMode's
  // mount/unmount/remount cycle: the teardown unregisters, then effects re-run
  // with no intervening render to re-register, so every request in the second
  // pass silently loses its Authorization header. Re-registering inside an
  // effect doesn't help either -- child effects run before parent ones, which
  // is the same ordering problem that forced registration into render above.
  // Nothing leaks by omitting it: a replacement provider overwrites the hooks
  // during its own render, and the accessors only read refs.

  async function login(email: string, password: string) {
    const tokens = await api.login(email, password)
    persist({
      email,
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
    })
  }

  async function signup(email: string, password: string, displayName: string) {
    await api.signup(email, password, displayName)
    // signup doesn't return tokens -- log in right after with the same
    // credentials so the caller only has to await one thing
    await login(email, password)
  }

  function logout() {
    persist(null)
  }

  return (
    <AuthContext.Provider value={{ session, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  )
}
