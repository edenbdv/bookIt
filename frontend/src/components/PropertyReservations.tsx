import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, createReview, listReservationsForProperty } from '../api/client'
import type { Reservation } from '../types'
import { formatPrice } from '../utils/format'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; reservations: Reservation[] }

// same four-state badge concept as BookingForm's STATUS_STYLES, simplified
// to just the badge since this list has no per-status container styling
const STATUS_BADGE: Record<Reservation['status'], string> = {
  pending: 'bg-amber-100 text-amber-800',
  confirmed: 'bg-green-100 text-green-800',
  cancelled: 'bg-gray-200 text-gray-600',
  completed: 'bg-blue-100 text-blue-800',
}

function ReviewForm({ reservationId, onDone }: { reservationId: string; onDone: () => void }) {
  const [rating, setRating] = useState(5)
  const [comment, setComment] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await createReview(reservationId, { rating, comment })
      onDone()
    } catch (err) {
      // covers the backend's duplicate-review 409 too -- its message is
      // clear enough to show as-is, no special-casing needed
      setError(err instanceof ApiError ? err.message : 'Could not submit this review.')
      setSubmitting(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mt-2.5 flex flex-col gap-2.5 rounded-lg border border-line bg-ground p-3"
    >
      <label className="flex items-center gap-2 text-sm text-ink">
        Rating
        <select
          value={rating}
          onChange={(e) => setRating(Number(e.target.value))}
          className="rounded-md border border-line bg-white px-2 py-1 text-sm focus:border-accent focus:outline-none"
        >
          {[5, 4, 3, 2, 1].map((n) => (
            <option key={n} value={n}>
              {n} star{n === 1 ? '' : 's'}
            </option>
          ))}
        </select>
      </label>
      <textarea
        required
        rows={2}
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="How was this guest?"
        className="rounded-md border border-line bg-white px-2.5 py-2 text-sm text-ink focus:border-accent focus:outline-none"
      />
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={submitting}
        className="self-start rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent-ink disabled:opacity-60"
      >
        {submitting ? 'Submitting...' : 'Submit review'}
      </button>
    </form>
  )
}

// BOOK-102: a host's bookings list for one property, with a review action on
// completed stays. Guest identity isn't shown beyond dates/status -- there's
// no user-lookup endpoint to resolve guest_id to a display name yet.
export function PropertyReservations({ propertyId }: { propertyId: string }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [reviewingId, setReviewingId] = useState<string | null>(null)
  const [reviewedIds, setReviewedIds] = useState<Set<string>>(new Set())

  useEffect(() => {
    let cancelled = false
    listReservationsForProperty(propertyId)
      .then((reservations) => {
        if (!cancelled) setState({ status: 'ready', reservations })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message = err instanceof ApiError ? err.message : 'Could not load reservations.'
        setState({ status: 'error', message })
      })
    return () => {
      cancelled = true
    }
  }, [propertyId])

  if (state.status === 'loading') {
    return <p className="mt-3 text-sm text-ink-soft">Loading reservations...</p>
  }
  if (state.status === 'error') {
    return (
      <p role="alert" className="mt-3 text-sm text-red-700">
        {state.message}
      </p>
    )
  }
  if (state.reservations.length === 0) {
    return <p className="mt-3 text-sm text-ink-soft">No reservations yet.</p>
  }

  return (
    <ul className="mt-3 space-y-2">
      {state.reservations.map((r) => (
        <li key={r.id} className="rounded-lg border border-line px-3 py-2.5 text-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-ink">
              {r.check_in} &rarr; {r.check_out} &middot; {r.guest_count} guest
              {r.guest_count === 1 ? '' : 's'} &middot; {formatPrice(r.total_cents)}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-semibold tracking-wide uppercase ${STATUS_BADGE[r.status]}`}
            >
              {r.status}
            </span>
          </div>

          {r.status === 'completed' && !reviewedIds.has(r.id) && reviewingId !== r.id && (
            <button
              type="button"
              onClick={() => setReviewingId(r.id)}
              className="mt-2 text-sm font-semibold text-accent-ink hover:underline"
            >
              Leave a review
            </button>
          )}
          {r.status === 'completed' && reviewingId === r.id && (
            <ReviewForm
              reservationId={r.id}
              onDone={() => {
                setReviewedIds((prev) => new Set(prev).add(r.id))
                setReviewingId(null)
              }}
            />
          )}
          {r.status === 'completed' && reviewedIds.has(r.id) && (
            <p className="mt-2 text-sm text-ink-soft">Review submitted.</p>
          )}
        </li>
      ))}
    </ul>
  )
}
