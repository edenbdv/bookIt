import { useEffect, useRef, useState } from 'react'
import { ApiError, cancelReservation, getProperty, listMyReservations, payReservation } from '../api/client'
import { ReviewForm } from '../components/ReviewForm'
import { Spinner } from '../components/Spinner'
import { useAuth } from '../context/useAuth'
import type { PropertyDetail, Reservation } from '../types'
import { formatPrice } from '../utils/format'
import { pollUntilResolved } from '../utils/pollReservation'

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; reservations: Reservation[]; propertiesById: Record<string, PropertyDetail> }

// same four-state badge concept as PropertyReservations/BookingForm
const STATUS_BADGE: Record<Reservation['status'], string> = {
  pending: 'bg-amber-100 text-amber-800',
  confirmed: 'bg-green-100 text-green-800',
  cancelled: 'bg-gray-200 text-gray-600',
  completed: 'bg-blue-100 text-blue-800',
}

// BOOK-98: 'polling' covers a payment that came back 'pending' -- distinct
// from 'paying' (the initial charge request itself is still in flight)
type RowAction = 'idle' | 'paying' | 'cancelling' | 'polling'

export function MyBookings() {
  const { session } = useAuth()
  const [state, setState] = useState<LoadState>({ status: 'loading' })
  const [rowActions, setRowActions] = useState<Record<string, RowAction>>({})
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [confirmCancelId, setConfirmCancelId] = useState<string | null>(null)
  // BOOK-86: which COMPLETED row is showing its review form, and which
  // ones already got one this session -- same pattern as PropertyReservations
  const [reviewingId, setReviewingId] = useState<string | null>(null)
  const [reviewedIds, setReviewedIds] = useState<Set<string>>(new Set())
  // populated once a cancel actually returns -- 0 is a real "no refund"
  // answer, not "unknown" (same convention as PropertyReservations)
  const [refundByReservationId, setRefundByReservationId] = useState<Record<string, number>>({})
  // BOOK-98: a poll can run for up to ~50s -- long enough that navigating
  // away mid-poll is realistic
  const unmountedRef = useRef(false)
  useEffect(() => {
    // reset on (re)mount -- StrictMode's dev-only double-invoke (mount ->
    // cleanup -> mount) would otherwise leave this stuck true from that
    // first, throwaway cleanup, silently killing every poll's first check
    unmountedRef.current = false
    return () => {
      unmountedRef.current = true
    }
  }, [])

  useEffect(() => {
    if (!session) return
    let cancelled = false

    listMyReservations()
      .then(async (reservations) => {
        // ReservationRead only carries property_id -- fetch each distinct
        // property once so the list can show a title/city, not a raw UUID
        const propertyIds = [...new Set(reservations.map((r) => r.property_id))]
        const properties = await Promise.all(propertyIds.map((id) => getProperty(id)))
        if (cancelled) return
        setState({
          status: 'ready',
          reservations,
          propertiesById: Object.fromEntries(properties.map((p) => [p.id, p])),
        })
      })
      .catch((err: unknown) => {
        if (cancelled) return
        const message = err instanceof ApiError ? err.message : 'Could not load your bookings.'
        setState({ status: 'error', message })
      })

    return () => {
      cancelled = true
    }
  }, [session])

  function updateStatus(reservationId: string, status: Reservation['status']) {
    setState((prev) =>
      prev.status === 'ready'
        ? {
            ...prev,
            reservations: prev.reservations.map((r) =>
              r.id === reservationId ? { ...r, status } : r,
            ),
          }
        : prev,
    )
  }

  // BOOK-98: shared by both paths that land on "a payment is already
  // resolving for this reservation" -- a fresh 'pending' response, and a
  // 409 from retrying while an earlier attempt is still stuck (below)
  async function pollAndSettle(reservation: Reservation) {
    setRowActions((prev) => ({ ...prev, [reservation.id]: 'polling' }))
    const resolved = await pollUntilResolved(reservation.id, () => unmountedRef.current)
    if (unmountedRef.current) return
    if (resolved === null) {
      setRowErrors((prev) => ({
        ...prev,
        [reservation.id]: 'This is taking longer than expected -- check back in a bit.',
      }))
      return
    }
    updateStatus(reservation.id, resolved.status)
    if (resolved.status !== 'confirmed') {
      setRowErrors((prev) => ({
        ...prev,
        [reservation.id]: 'Payment was declined -- this reservation has been released.',
      }))
    }
  }

  // mirrors BookingForm's own confirm() -- a 201 with status 'succeeded'/
  // 'failed' is a hard guarantee confirm/cancel already happened server-side
  async function handlePay(reservation: Reservation) {
    setRowActions((prev) => ({ ...prev, [reservation.id]: 'paying' }))
    setRowErrors((prev) => ({ ...prev, [reservation.id]: '' }))
    try {
      const idempotencyKey = crypto.randomUUID()
      const payment = await payReservation(reservation.id, idempotencyKey)
      if (payment.status === 'pending') {
        await pollAndSettle(reservation)
        return
      }
      const succeeded = payment.status === 'succeeded'
      updateStatus(reservation.id, succeeded ? 'confirmed' : 'cancelled')
      if (!succeeded) {
        setRowErrors((prev) => ({
          ...prev,
          [reservation.id]: 'Payment was declined -- this reservation has been released.',
        }))
      }
    } catch (err) {
      // BOOK-100's guard: an earlier attempt already timed out and is still
      // resolving -- go poll it instead of dead-ending on the raw 409
      if (err instanceof ApiError && err.status === 409) {
        await pollAndSettle(reservation)
        return
      }
      setRowErrors((prev) => ({
        ...prev,
        [reservation.id]: err instanceof ApiError ? err.message : 'Could not confirm this reservation.',
      }))
    } finally {
      setRowActions((prev) => ({ ...prev, [reservation.id]: 'idle' }))
    }
  }

  async function handleCancel(reservationId: string) {
    setRowActions((prev) => ({ ...prev, [reservationId]: 'cancelling' }))
    setRowErrors((prev) => ({ ...prev, [reservationId]: '' }))
    try {
      const cancelled = await cancelReservation(reservationId)
      updateStatus(reservationId, cancelled.status)
      setRefundByReservationId((prev) => ({ ...prev, [reservationId]: cancelled.refund_amount_cents }))
      setConfirmCancelId(null)
    } catch (err) {
      setRowErrors((prev) => ({
        ...prev,
        [reservationId]: err instanceof ApiError ? err.message : 'Could not cancel this reservation.',
      }))
    } finally {
      setRowActions((prev) => ({ ...prev, [reservationId]: 'idle' }))
    }
  }

  if (!session) {
    return (
      <main className="mx-auto max-w-3xl px-8 py-10">
        <p className="text-sm text-ink-soft">Log in to see your bookings.</p>
      </main>
    )
  }

  return (
    <main className="mx-auto max-w-3xl px-8 py-7">
      <h1 className="font-display text-2xl font-medium text-ink">My Bookings</h1>

      {state.status === 'loading' && <p className="mt-6 text-sm text-ink-soft">Loading your bookings...</p>}
      {state.status === 'error' && (
        <p role="alert" className="mt-6 text-sm text-red-700">
          {state.message}
        </p>
      )}
      {state.status === 'ready' && state.reservations.length === 0 && (
        <p className="mt-6 text-sm text-ink-soft">You haven't booked anything yet.</p>
      )}

      {state.status === 'ready' && state.reservations.length > 0 && (
        <ul className="mt-6 space-y-3">
          {state.reservations.map((r) => {
            const property = state.propertiesById[r.property_id]
            const action = rowActions[r.id] ?? 'idle'
            const error = rowErrors[r.id]
            return (
              <li key={r.id} className="rounded-2xl border border-line bg-white p-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="font-display text-lg font-medium text-ink">
                      {property ? property.title : 'Property'}
                    </h2>
                    {property && (
                      <p className="text-sm text-ink-soft">
                        {property.city}, {property.country}
                      </p>
                    )}
                    <p className="mt-1 text-sm text-ink">
                      {r.check_in} &rarr; {r.check_out} &middot; {r.guest_count} guest
                      {r.guest_count === 1 ? '' : 's'} &middot; {formatPrice(r.total_cents)}
                    </p>
                  </div>
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-semibold tracking-wide uppercase ${STATUS_BADGE[r.status]}`}
                  >
                    {r.status}
                  </span>
                </div>

                {action === 'polling' && (
                  <p className="mt-2 flex items-center gap-2 text-sm text-amber-700">
                    <Spinner /> Still confirming your payment -- this can take a little while.
                  </p>
                )}

                {error && (
                  <p role="alert" className="mt-2 text-sm text-red-700">
                    {error}
                  </p>
                )}

                {r.status === 'cancelled' && refundByReservationId[r.id] !== undefined && (
                  <p className="mt-2 text-sm text-ink-soft">
                    {refundByReservationId[r.id] > 0
                      ? `Refund: ${formatPrice(refundByReservationId[r.id])}, based on this property's cancellation policy.`
                      : 'No refund for this cancellation.'}
                  </p>
                )}

                {(r.status === 'pending' || r.status === 'confirmed') && (
                  <div className="mt-3 flex items-center gap-2">
                    {r.status === 'pending' && (
                      <button
                        type="button"
                        disabled={action !== 'idle'}
                        onClick={() => handlePay(r)}
                        className="flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-sm font-semibold text-white hover:bg-accent-ink disabled:opacity-60"
                      >
                        {(action === 'paying' || action === 'polling') && <Spinner />}
                        {action === 'paying'
                          ? 'Processing payment...'
                          : action === 'polling'
                            ? 'Confirming...'
                            : 'Pay & confirm'}
                      </button>
                    )}

                    {confirmCancelId === r.id ? (
                      <>
                        <span className="text-sm text-ink-soft">Cancel this booking?</span>
                        <button
                          type="button"
                          disabled={action !== 'idle'}
                          onClick={() => handleCancel(r.id)}
                          className="flex items-center gap-2 rounded-lg bg-red-600 px-2.5 py-1 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-60"
                        >
                          {action === 'cancelling' && <Spinner />}
                          {action === 'cancelling' ? 'Cancelling...' : 'Yes, cancel'}
                        </button>
                        <button
                          type="button"
                          disabled={action !== 'idle'}
                          onClick={() => setConfirmCancelId(null)}
                          className="rounded-lg border border-line px-2.5 py-1 text-sm font-semibold text-ink hover:bg-ground"
                        >
                          Keep it
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        disabled={action !== 'idle'}
                        onClick={() => setConfirmCancelId(r.id)}
                        className="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold text-ink hover:bg-ground disabled:opacity-60"
                      >
                        Cancel
                      </button>
                    )}
                  </div>
                )}

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
                    placeholder="How was your stay?"
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
            )
          })}
        </ul>
      )}
    </main>
  )
}
