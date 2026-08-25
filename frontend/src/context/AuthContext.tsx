import { useState, type ReactNode } from 'react'
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

  function persist(next: Session | null) {
    setSession(next)
    if (next) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next))
    } else {
      localStorage.removeItem(STORAGE_KEY)
    }
  }

  async function login(email: string, password: string) {
    const tokens = await api.login(email, password)
    persist({ email, accessToken: tokens.access_token })
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
