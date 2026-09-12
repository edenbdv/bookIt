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
  // BOOK-90 availability window: ISO yyyy-mm-dd, matching the backend's
  // `date` params. The API rejects these unless BOTH are present and
  // check_out is strictly after check_in, so treat them as a pair --
  // TypeScript can't express that constraint on two optional fields.
  check_in?: string
  check_out?: string
  sort?: PropertySort
}

// app/reservation/models.py's ReservationStatus -- keep in sync by hand,
// same as everything else in this file
export type ReservationStatus = 'pending' | 'confirmed' | 'cancelled' | 'completed'

export interface ReservationCreate {
  property_id: string
  check_in: string
  check_out: string
  guest_count: number
}

export interface Reservation {
  id: string
  property_id: string
  guest_id: string
  check_in: string
  check_out: string
  guest_count: number
  // snapshot at booking time (app/reservation/service.py) -- never recompute
  // this from the property's current price_per_night_cents
  total_cents: number
  status: ReservationStatus
  // confirm does NOT clear this (BOOK-83) -- a confirmed reservation still
  // carries its original (now past) hold deadline. Only meaningful when
  // status === 'pending'; don't render it otherwise.
  expires_at: string | null
  created_at: string
}

// app/payment/models.py's PaymentStatus -- keep in sync by hand
export type PaymentStatus = 'pending' | 'succeeded' | 'failed'

export interface Payment {
  id: string
  reservation_id: string
  kind: 'charge' | 'refund'
  amount_cents: number
  status: PaymentStatus
  created_at: string
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
