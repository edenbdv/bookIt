import { createContext } from 'react'

export interface Session {
  email: string
  // BOOK-102: the current user's own id, e.g. for filtering "my properties"
  // by host_id. Fetched from GET /auth/me right after login/signup, since
  // the access token itself carries no user-facing identity claims.
  userId: string
  accessToken: string
  // optional because sessions persisted before refresh support existed won't
  // have one -- those simply can't be renewed and get cleared on first 401
  refreshToken?: string
}

export interface AuthContextValue {
  session: Session | null
  login: (email: string, password: string) => Promise<void>
  signup: (email: string, password: string, displayName: string) => Promise<void>
  logout: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)
