import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, cancelReservation, createReservation, payReservation } from '../api/client'
import { useAuth } from '../context/useAuth'
import { Spinner } from './Spinner'
import type { PropertyDetail, Reservation } from '../types'
import { formatPrice } from '../utils/format'
import { todayIso } from '../utils/dates'
import { pollUntilResolved } from '../utils/pollReservation'

interface Props {
  property: PropertyDetail
}

// BOOK-83: a reservation is either being set up (the form), or it exists and
// the user can act on it (confirm the stub / cancel). `action` tracks an
// in-flight sub-request against an existing reservation, separate from the
// reservation's own `status` -- e.g. status stays 'pending' the entire time
// a confirm request is in flight.
type BookingState =
  | { kind: 'form' }
  | { kind: 'submitting' }
  | {
      kind: 'reservation'
      reservation: Reservation
      // BOOK-98: 'polling' covers a payment that came back 'pending' --
      // distinct from 'confirming' (the initial charge request itself is
      // still in flight) so the two show different messages
      action: 'idle' | 'confirming' | 'cancelling' | 'polling'
      actionError?: string
      // BOOK-76/99: set once a cancel actually returns -- 0 is a real "no
      // refund" answer, not "unknown", so this is only absent before any
      // cancel has happened yet
      refundAmountCents?: number
    }
  | { kind: 'error'; message: string }

function nightsBetween(checkIn: string, checkOut: string): number {
  const oneDayMs = 24 * 60 * 60 * 1000
  return Math.round((new Date(checkOut).getTime() - new Date(checkIn).getTime()) / oneDayMs)
}

function formatExpiry(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

// visually distinct per status so "confirmed" reads as a positive outcome
// and "cancelled" as a settled, de-emphasized one at a glance, not just text
const STATUS_STYLES: Record<Reservation['status'], { container: string; badge: string }> = {
  pending: { container: 'border-amber-200 bg-amber-50', badge: 'bg-amber-100 text-amber-800' },
  confirmed: { container: 'border-green-200 bg-green-50', badge: 'bg-green-100 text-green-800' },
  cancelled: { container: 'border-gray-200 bg-gray-50', badge: 'bg-gray-200 text-gray-600' },
  completed: { container: 'border-blue-200 bg-blue-50', badge: 'bg-blue-100 text-blue-800' },
}

export function BookingForm({ property }: Props) {
  const { session } = useAuth()
  const [checkIn, setCheckIn] = useState('')
  const [checkOut, setCheckOut] = useState('')
  const [guestCount, setGuestCount] = useState('1')
  const [validationError, setValidationError] = useState<string | null>(null)
  const [state, setState] = useState<BookingState>({ kind: 'form' })
  // BOOK-98: a poll can run for up to ~50s -- long enough that a guest
  // navigating away mid-poll is realistic, unlike the sub-2s requests this
  // form otherwise makes
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

  if (!session) {
    return (
      <p className="mt-4 text-sm text-ink-soft">
        <Link to="/login" className="font-semibold text-accent-ink underline">
          Log in
        </Link>{' '}
        to book this property.
      </p>
    )
  }

  // mirrors GET /properties' own two rules (app/property/router.py) and
  // POST /reservations' guest_count check (app/reservation/service.py) --
  // client-side validation, server stays authoritative either way
  function validate(): string | null {
    if (!checkIn || !checkOut) return 'Pick both a check-in and a check-out date.'
    if (checkIn < todayIso()) return 'Check-in cannot be in the past.'
    if (checkOut <= checkIn) return 'Check-out must be after check-in.'
    const guests = Number(guestCount)
    if (!Number.isInteger(guests) || guests < 1) return 'Guest count must be at least 1.'
    if (guests > property.max_guests) return `This property sleeps at most ${property.max_guests} guests.`
    return null
  }

  async function submit(e: FormEvent) {
    e.preventDefault()
    const error = validate()
    setValidationError(error)
    if (error) return

    setState({ kind: 'submitting' })
    try {
      // fresh per attempt -- reused only if this exact attempt were retried,
      // which this form doesn't currently do automatically (BOOK-83)
      const idempotencyKey = crypto.randomUUID()
      const reservation = await createReservation(
        { property_id: property.id, check_in: checkIn, check_out: checkOut, guest_count: Number(guestCount) },
        idempotencyKey,
      )
      setState({ kind: 'reservation', reservation, action: 'idle' })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Could not create this reservation.'
      setState({ kind: 'error', message })
    }
  }

  // BOOK-97. payReservation() triggers BOOK-54's saga -- a 201 response with
  // status 'succeeded'/'failed' is a hard guarantee confirm/cancel already
  // happened server-side (see client.ts's payReservation comment), so it's
  // safe to derive the reservation's new status here rather than re-fetch it.
  // BOOK-98: shared by both paths that land on "a payment is already
  // resolving for this reservation" -- a fresh 'pending' response, and a
  // 409 from retrying while an earlier attempt is still stuck (below)
  async function pollAndSettle(reservation: Reservation) {
    setState({ kind: 'reservation', reservation, action: 'polling' })
    const resolved = await pollUntilResolved(reservation.id, () => unmountedRef.current)
    if (unmountedRef.current) return
    if (resolved === null) {
      setState({
        kind: 'reservation',
        reservation,
        action: 'idle',
        actionError: 'This is taking longer than expected -- check back in a bit.',
      })
      return
    }
    const resolvedSucceeded = resolved.status === 'confirmed'
    setState({
      kind: 'reservation',
      reservation: resolved,
      action: 'idle',
      actionError: resolvedSucceeded
        ? undefined
        : 'Payment was declined -- this reservation has been released.',
    })
  }

  async function confirm(reservation: Reservation) {
    setState({ kind: 'reservation', reservation, action: 'confirming' })
    try {
      // fresh per attempt, same convention as submit()'s createReservation call
      const idempotencyKey = crypto.randomUUID()
      const payment = await payReservation(reservation.id, idempotencyKey)
      if (payment.status === 'pending') {
        // a persistent processing state, not a one-off error that resets to
        // idle -- poll until BOOK-55's webhook (or BOOK-101's reconciliation
        // worker) resolves it, or give up after ~50s
        await pollAndSettle(reservation)
        return
      }
      const succeeded = payment.status === 'succeeded'
      const updated: Reservation = { ...reservation, status: succeeded ? 'confirmed' : 'cancelled' }
      setState({
        kind: 'reservation',
        reservation: updated,
        action: 'idle',
        // BOOK-52's mock declines ~8% of charges -- without this, a decline
        // just silently flips to "cancelled" with no explanation of why
        actionError: succeeded
          ? undefined
          : 'Payment was declined -- this reservation has been released.',
      })
    } catch (err) {
      // BOOK-100's guard: an earlier attempt already timed out and is still
      // resolving -- go poll it instead of dead-ending on the raw 409
      if (err instanceof ApiError && err.status === 409) {
        await pollAndSettle(reservation)
        return
      }
      const message = err instanceof ApiError ? err.message : 'Could not confirm this reservation.'
      setState({ kind: 'reservation', reservation, action: 'idle', actionError: message })
    }
  }

  async function cancel(reservation: Reservation) {
    setState({ kind: 'reservation', reservation, action: 'cancelling' })
    try {
      const cancelled = await cancelReservation(reservation.id)
      setState({
        kind: 'reservation',
        reservation: cancelled,
        action: 'idle',
        refundAmountCents: cancelled.refund_amount_cents,
      })
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Could not cancel this reservation.'
      setState({ kind: 'reservation', reservation, action: 'idle', actionError: message })
    }
  }

  if (state.kind === 'reservation') {
    const { reservation, action, actionError, refundAmountCents } = state
    const styles = STATUS_STYLES[reservation.status]
    return (
      <div className={`mt-4 rounded-2xl border p-4 ${styles.container}`}>
        <p className="text-sm text-ink">
          Reservation{' '}
          <span className={`rounded-full px-2 py-0.5 text-xs font-semibold tracking-wide uppercase ${styles.badge}`}>
            {reservation.status}
          </span>{' '}
          &middot; {reservation.check_in} &rarr; {reservation.check_out} &middot; {reservation.guest_count} guest
          {reservation.guest_count === 1 ? '' : 's'} &middot; {formatPrice(reservation.total_cents)} total
        </p>

        {reservation.status === 'pending' && reservation.expires_at && (
          <p className="mt-2 text-sm text-amber-700">
            Held until {formatExpiry(reservation.expires_at)} -- confirm before then or it's released
            automatically.
          </p>
        )}

        {action === 'polling' && (
          <p className="mt-2 flex items-center gap-2 text-sm text-amber-700">
            <Spinner /> Still confirming your payment -- this can take a little while.
          </p>
        )}

        {actionError && (
          <p role="alert" className="mt-2 text-sm text-red-700">
            {actionError}
          </p>
        )}

        {/* BOOK-99: only after a cancel this page itself just performed --
            refund_amount_cents isn't part of a plain reservation fetch */}
        {reservation.status === 'cancelled' && refundAmountCents !== undefined && (
          <p className="mt-2 text-sm text-ink-soft">
            {refundAmountCents > 0
              ? `Refund: ${formatPrice(refundAmountCents)}, based on this property's cancellation policy.`
              : // 0 covers two different cases the frontend can't tell apart from
                // this number alone -- never charged in the first place, or charged
                // but the policy/timing zeroed it out -- so this doesn't claim which
                'No refund for this cancellation.'}
          </p>
        )}

        <div className="mt-4 flex gap-3">
          {reservation.status === 'pending' && (
            <button
              type="button"
              disabled={action !== 'idle'}
              onClick={() => confirm(reservation)}
              className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-ink disabled:opacity-70"
            >
              {(action === 'confirming' || action === 'polling') && <Spinner />}
              {action === 'confirming' ? 'Processing payment...' : action === 'polling' ? 'Confirming...' : 'Pay & confirm'}
            </button>
          )}
          {(reservation.status === 'pending' || reservation.status === 'confirmed') && (
            <button
              type="button"
              disabled={action !== 'idle'}
              onClick={() => cancel(reservation)}
              className="flex items-center gap-2 rounded-lg border border-line px-4 py-2 text-sm font-semibold text-ink hover:bg-ground disabled:opacity-70"
            >
              {action === 'cancelling' && <Spinner />}
              {action === 'cancelling' ? 'Cancelling...' : 'Cancel'}
            </button>
          )}
        </div>
      </div>
    )
  }

  const nights = checkIn && checkOut && checkOut > checkIn ? nightsBetween(checkIn, checkOut) : 0
  const submitting = state.kind === 'submitting'

  return (
    // noValidate: this form has its own validate() with friendlier, styled
    // messages -- without it, the browser's native constraint validation
    // (triggered by the date/number inputs' min/max) intercepts submit
    // before onSubmit ever runs, silently, with no visible error and no
    // network call. Found by testing an out-of-range guest count.
    <form onSubmit={submit} noValidate className="mt-5">
      <div className="grid grid-cols-2 gap-2.5">
        <label className="flex flex-col gap-1.5 text-[11.5px] font-semibold tracking-wide text-ink-soft uppercase">
          Check in
          <input
            className="rounded-[9px] border border-line bg-ground px-2.5 py-2 text-sm font-sans text-ink normal-case focus:ring-2 focus:ring-accent focus:outline-none"
            type="date"
            dir="ltr"
            min={todayIso()}
            value={checkIn}
            onChange={(e) => setCheckIn(e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1.5 text-[11.5px] font-semibold tracking-wide text-ink-soft uppercase">
          Check out
          <input
            className="rounded-[9px] border border-line bg-ground px-2.5 py-2 text-sm font-sans text-ink normal-case focus:ring-2 focus:ring-accent focus:outline-none"
            type="date"
            dir="ltr"
            min={checkIn || todayIso()}
            value={checkOut}
            onChange={(e) => setCheckOut(e.target.value)}
          />
        </label>
        <label className="col-span-2 flex flex-col gap-1.5 text-[11.5px] font-semibold tracking-wide text-ink-soft uppercase">
          Guests
          <input
            className="rounded-[9px] border border-line bg-ground px-2.5 py-2 text-sm font-sans text-ink normal-case focus:ring-2 focus:ring-accent focus:outline-none"
            type="number"
            min={1}
            max={property.max_guests}
            value={guestCount}
            onChange={(e) => setGuestCount(e.target.value)}
          />
        </label>
      </div>

      {nights > 0 && (
        <div className="mt-4 border-t border-line pt-3.5">
          <div className="flex justify-between text-sm text-ink-soft tabular-nums">
            <span>
              {formatPrice(property.price_per_night_cents)} &times; {nights} night{nights === 1 ? '' : 's'}
            </span>
            <span>{formatPrice(nights * property.price_per_night_cents)}</span>
          </div>
          <div className="mt-2 flex justify-between border-t border-line pt-2 text-[15px] font-semibold text-ink tabular-nums">
            <span>Total</span>
            <span>{formatPrice(nights * property.price_per_night_cents)}</span>
          </div>
        </div>
      )}

      <button
        type="submit"
        disabled={submitting}
        className="mt-4 w-full rounded-[9px] bg-accent px-4 py-3 text-[15px] font-semibold text-white hover:bg-accent-ink disabled:opacity-50"
      >
        {submitting ? 'Booking...' : 'Reserve'}
      </button>

      {validationError && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {validationError}
        </p>
      )}
      {state.kind === 'error' && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {state.message}
        </p>
      )}
    </form>
  )
}
