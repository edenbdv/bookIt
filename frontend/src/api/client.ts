import type {
  Payment,
  PropertyDetail,
  PropertyFilters,
  PropertyListItem,
  Reservation,
  ReservationCreate,
  Review,
  Tokens,
  User,
} from '../types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/**
 * Auth plumbing supplied by AuthProvider at mount.
 *
 * This module can't read React state, and duplicating the localStorage key
 * here would give the session two owners. Instead the provider — which owns
 * the session — registers accessors, and this module just calls them.
 */
export interface AuthHooks {
  getAccessToken: () => string | null
  /** Returns a fresh access token, or null if the session can't be renewed. */
  refreshAccessToken: () => Promise<string | null>
}

let authHooks: AuthHooks | null = null

export function configureAuth(hooks: AuthHooks | null): void {
  authHooks = hooks
}

interface RequestOptions {
  /** Skip auth entirely. Set on /auth/* so a 401 there can't recurse into refresh. */
  anonymous?: boolean
}

async function request<T>(
  path: string,
  init?: RequestInit,
  { anonymous = false }: RequestOptions = {},
): Promise<T> {
  const send = (token: string | null) => {
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...(init?.headers as Record<string, string> | undefined),
    }
    // only when a session exists -- public endpoints must keep working
    // logged out, and an empty Bearer header is worse than none
    if (token) headers.Authorization = `Bearer ${token}`
    return fetch(`${API_BASE_URL}${path}`, { ...init, headers })
  }

  const token = anonymous ? null : (authHooks?.getAccessToken() ?? null)
  let res = await send(token)

  // access tokens last 15 minutes (settings.access_token_expire_minutes), so
  // expiring mid-session is routine rather than exceptional: renew once and
  // replay the request before surfacing an error. Only when we actually sent
  // a token -- a 401 without one is a genuine "you must log in".
  if (res.status === 401 && !anonymous && token && authHooks) {
    const fresh = await authHooks.refreshAccessToken()
    if (fresh) res = await send(fresh)
  }

  if (!res.ok) {
    // FastAPI's HTTPException body shape is {"detail": "..."} -- fall back to
    // the status text if the body isn't JSON or doesn't have that shape
    const detail = await res
      .json()
      .then((body) => body.detail)
      .catch(() => null)
    throw new ApiError(res.status, detail || res.statusText || 'Request failed')
  }

  // 204 No Content has no body to parse
  if (res.status === 204) return undefined as T

  return res.json() as Promise<T>
}

// every /auth/* call is `anonymous`: they establish or renew the session
// rather than consuming it, and a 401 from one must surface to the caller
// instead of triggering a refresh that would recurse straight back here

export function signup(email: string, password: string, displayName: string): Promise<User> {
  return request<User>(
    '/auth/signup',
    { method: 'POST', body: JSON.stringify({ email, password, display_name: displayName }) },
    { anonymous: true },
  )
}

export function login(email: string, password: string): Promise<Tokens> {
  return request<Tokens>(
    '/auth/login',
    { method: 'POST', body: JSON.stringify({ email, password }) },
    { anonymous: true },
  )
}

export function refreshTokens(refreshToken: string): Promise<Tokens> {
  // mints a fresh pair, but does NOT invalidate the refresh token spent to
  // get it -- that one stays valid for its full refresh_token_expire_days
  // (verified: replaying the same one repeatedly still returns 200). Rotation
  // and revocation are BOOK-78. So storing the returned refresh_token is
  // housekeeping, not a security boundary.
  return request<Tokens>(
    '/auth/refresh',
    { method: 'POST', body: JSON.stringify({ refresh_token: refreshToken }) },
    { anonymous: true },
  )
}

export function listProperties(filters: PropertyFilters): Promise<PropertyListItem[]> {
  const params = new URLSearchParams()
  params.set('limit', '40')
  if (filters.city) params.set('city', filters.city)
  if (filters.guests) params.set('guests', String(filters.guests))
  if (filters.min_price_cents != null) params.set('min_price_cents', String(filters.min_price_cents))
  if (filters.max_price_cents != null) params.set('max_price_cents', String(filters.max_price_cents))
  // sent as a pair, unlike the filters above: half a date range is a
  // guaranteed 422 ("check_in and check_out must be provided together"),
  // so a partial one is dropped rather than turned into a failed request.
  // Callers validate first -- SearchBar surfaces the message itself.
  if (filters.check_in && filters.check_out) {
    params.set('check_in', filters.check_in)
    params.set('check_out', filters.check_out)
  }
  if (filters.sort) params.set('sort', filters.sort)

  return request<PropertyListItem[]>(`/properties?${params.toString()}`)
}

export function getProperty(id: string): Promise<PropertyDetail> {
  return request<PropertyDetail>(`/properties/${id}`)
}

export function getPropertyReviews(propertyId: string): Promise<Review[]> {
  return request<Review[]>(`/properties/${propertyId}/reviews?limit=50`)
}

export function listCities(): Promise<string[]> {
  return request<string[]>('/properties/cities')
}

// BOOK-83. Kept separate from payReservation below -- two distinct calls,
// not one "book it" action. idempotencyKey is caller-supplied (a fresh UUID
// per attempt, reused only when retrying that exact attempt) since it's a
// request-level concern, not something this client should silently generate
// on retry.
export function createReservation(
  data: ReservationCreate,
  idempotencyKey: string,
): Promise<Reservation> {
  return request<Reservation>('/reservations', {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

// BOOK-97. Replaces the old confirmReservationStub -- POST /payments (BOOK-53)
// triggers the real saga (BOOK-54): a succeeded charge confirms the
// reservation, a failed one cancels it, both inside the same request. There's
// no GET /reservations/:id yet (BOOK-85) to re-fetch the result, so callers
// derive the new status from the returned Payment themselves -- see
// BookingForm.tsx's confirm(). idempotencyKey follows createReservation's
// same convention: a fresh UUID per attempt.
export function payReservation(reservationId: string, idempotencyKey: string): Promise<Payment> {
  return request<Payment>('/payments', {
    method: 'POST',
    body: JSON.stringify({ reservation_id: reservationId }),
    headers: { 'Idempotency-Key': idempotencyKey },
  })
}

export function cancelReservation(id: string): Promise<Reservation> {
  return request<Reservation>(`/reservations/${id}/cancel`, { method: 'POST' })
}
