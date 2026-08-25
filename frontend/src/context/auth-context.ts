import { createContext } from 'react'

export interface Session {
  email: string
  accessToken: string
}

export interface AuthContextValue {
  session: Session | null
  login: (email: string, password: string) => Promise<void>
  signup: (email: string, password: string, displayName: string) => Promise<void>
  logout: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)
