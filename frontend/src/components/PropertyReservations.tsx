import { useEffect, useState } from 'react'
import { ApiError, hostCancelReservation, listReservationsForProperty } from '../api/client'
import { ReviewForm } from './ReviewForm'
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

// BOOK-102: a host's bookings list for one property, with a review action on
// completed stays. Guest identity isn't shown beyond dates/status -- there's
// no user-lookup endpoint to resolve guest_id to a display name yet.
export function PropertyReservations({ propertyId }: { propertyId: string }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [reviewingId, setReviewingId] = useState<string | null>(null)
  const [reviewedIds, setReviewedIds] = useState<Set<string>>(new Set())
  // BOOK-80: inline confirm (no browser confirm()/alert()), same pattern as
  // HostProperties' delete-property flow
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null)
  const [cancellingId, setCancellingId] = useState<string | null>(null)
  const [cancelError, setCancelError] = useState<string | null>(null)
  // populated once a cancel actually returns -- 0 is a real "no refund"
  // answer (e.g. a PENDING reservation was never charged), not "unknown"
  const [refundByReservationId, setRefundByReservationId] = useState<Record<string, number>>({})

  async function handleCancel(reservationId: string) {
    setCancellingId(reservationId)
    setCancelError(null)
    try {
      const cancelled = await hostCancelReservation(reservationId)
      setState((prev) =>
        prev.status === 'ready'
          ? {
              status: 'ready',
              reservations: prev.reservations.map((r) =>
                r.id === reservationId ? { ...r, status: cancelled.status } : r,
              ),
            }
          : prev,
      )
      setRefundByReservationId((prev) => ({ ...prev, [reservationId]: cancelled.refund_amount_cents }))
      setConfirmCancelId(null)
    } catch (err) {
      setCancelError(err instanceof ApiError ? err.message : 'Could not cancel this reservation.')
    } finally {
      setCancellingId(null)
    }
  }

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
              placeholder="How was this guest?"
              onDone={() => {
                setReviewedIds((prev) => new Set(prev).add(r.id))
                setReviewingId(null)
              }}
            />
          )}
          {r.status === 'completed' && reviewedIds.has(r.id) && (
            <p className="mt-2 text-sm text-ink-soft">Review submitted.</p>
          )}

          {(r.status === 'pending' || r.status === 'confirmed') &&
            (confirmCancelId === r.id ? (
              <div className="mt-2 flex items-center gap-2">
                <span className="text-sm text-ink-soft">Cancel this reservation?</span>
                <button
                  type="button"
                  disabled={cancellingId === r.id}
                  onClick={() => handleCancel(r.id)}
                  className="rounded-lg bg-red-600 px-2.5 py-1 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-60"
                >
                  {cancellingId === r.id ? 'Cancelling...' : 'Yes, cancel'}
                </button>
                <button
                  type="button"
                  disabled={cancellingId === r.id}
                  onClick={() => setConfirmCancelId(null)}
                  className="rounded-lg border border-line px-2.5 py-1 text-sm font-semibold text-ink hover:bg-ground"
                >
                  Keep it
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmCancelId(r.id)}
                className="mt-2 text-sm font-semibold text-red-700 hover:underline"
              >
                Cancel reservation
              </button>
            ))}
          {cancelError && confirmCancelId === r.id && (
            <p role="alert" className="mt-2 text-sm text-red-700">
              {cancelError}
            </p>
          )}
          {r.status === 'cancelled' && refundByReservationId[r.id] !== undefined && (
            <p className="mt-2 text-sm text-ink-soft">
              {refundByReservationId[r.id] > 0
                ? `Refunded ${formatPrice(refundByReservationId[r.id])} in full.`
                : 'No refund -- this reservation was never charged.'}
            </p>
          )}
        </li>
      ))}
    </ul>
  )
}
