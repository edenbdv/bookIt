import type { PropertyDetail, PropertyFilters, PropertyListItem, Review, Tokens, User } from '../types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })

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

export function signup(email: string, password: string, displayName: string): Promise<User> {
  return request<User>('/auth/signup', {
    method: 'POST',
    body: JSON.stringify({ email, password, display_name: displayName }),
  })
}

export function login(email: string, password: string): Promise<Tokens> {
  return request<Tokens>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
}

export function listProperties(filters: PropertyFilters): Promise<PropertyListItem[]> {
  const params = new URLSearchParams()
  params.set('limit', '40')
  if (filters.city) params.set('city', filters.city)
  if (filters.guests) params.set('guests', String(filters.guests))
  if (filters.min_price_cents != null) params.set('min_price_cents', String(filters.min_price_cents))
  if (filters.max_price_cents != null) params.set('max_price_cents', String(filters.max_price_cents))
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
