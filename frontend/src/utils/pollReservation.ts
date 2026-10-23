import { getReservation } from '../api/client'
import type { Reservation } from '../types'

// BOOK-98: shared by BookingForm and MyBookings -- both hit the identical
// "payment came back pending" gap. BOOK-65: now also every successful or
// failed payment, since the reservation's status follows a few seconds
// later over Kafka. ~50s total before giving up: long enough
// to cover BOOK-55's webhook actually firing, short enough not to leave a
// guest staring at a spinner forever if nothing ever resolves it (see
// property-decisions.md -- the webhook is manual-trigger-only in dev).
const POLL_INTERVAL_MS = 2500
const MAX_ATTEMPTS = 20

/**
 * Polls the reservation until its status leaves 'pending', or gives up.
 * Returns the resolved reservation, or null if it gave up still pending.
 * `isCancelled` is checked between polls so a caller can stop this early
 * (component unmounted, guest navigated away) without a stray state update.
 */
export async function pollUntilResolved(
  reservationId: string,
  isCancelled: () => boolean,
): Promise<Reservation | null> {
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    // BOOK-65: first check right away, not after a wait -- it's now the
    // normal path after every payment, and often already resolved
    if (attempt > 0) await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
    if (isCancelled()) return null

    try {
      const reservation = await getReservation(reservationId)
      if (reservation.status !== 'pending') return reservation
    } catch {
      // a transient fetch failure shouldn't end the poll -- just retry next tick
    }
  }
  return null
}
