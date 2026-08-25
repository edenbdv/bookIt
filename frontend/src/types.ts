// Mirrors app/auth/schemas.py and app/property/schemas.py -- kept in sync by
// hand since there's no shared codegen between the FastAPI backend and this
// frontend (OpenAPI-generated types would be the next step if this grew).

export interface User {
  id: string
  email: string
  // null for accounts created before display_name existed
  display_name: string | null
  role: 'user' | 'admin'
  created_at: string
}

export interface Tokens {
  access_token: string
  refresh_token: string
  token_type: string
}

export interface PropertyPhoto {
  id: string
  property_id: string
  url: string
  position: number
  created_at: string
}

export interface PropertyBase {
  id: string
  host_id: string
  title: string
  description: string
  city: string
  country: string
  max_guests: number
  price_per_night_cents: number
  created_at: string
  // BOOK-77 review aggregate, on both the list and detail responses (but not
  // create/update). null rating_avg means "no reviews yet", which the UI
  // shows as "New" rather than as a zero score.
  rating_avg: number | null
  review_count: number
}

export interface PropertyListItem extends PropertyBase {
  cover_photo: PropertyPhoto | null
}

export interface PropertyDetail extends PropertyBase {
  photos: PropertyPhoto[]
}

export type PropertySort = 'price' | 'recommended'

export interface PropertyFilters {
  city?: string
  guests?: number
  min_price_cents?: number
  max_price_cents?: number
  sort?: PropertySort
}

export interface Review {
  id: string
  reservation_id: string
  author_id: string
  // the author's display_name. null for authors who predate the field --
  // the UI falls back to "Guest". Never their email: this comes from a
  // public, unauthenticated endpoint.
  author_name: string | null
  rating: number
  comment: string
  created_at: string
}
