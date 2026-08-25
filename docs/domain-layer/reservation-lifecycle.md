# Reservation lifecycle, end to end

Ties together `POST /reservations` (`BOOK-34`/`35`/`36`), the transition table (`BOOK-37`), and cancellation (`BOOK-38`) into one picture -- what actually happens to a `Reservation` row from creation to its final state, and which parts exist today vs. are still stubbed out for a later epic.

**Prerequisite:** [`state-machine-basics.md`](state-machine-basics.md) (the transition table itself), [`../db/exclude-constraint-basics.md`](../db/exclude-constraint-basics.md) (why `PENDING` already blocks overlapping dates), [`../db/idempotency-basics.md`](../db/idempotency-basics.md) (the `Idempotency-Key` header on step 1).

## The flow

1. **Guest requests a booking** -- `POST /reservations`. A `Reservation` row is created immediately, status `PENDING`, with `expires_at` set `PENDING_HOLD_MINUTES` (15) out.
2. **The dates are locked in from that instant.** Even though it's only `PENDING`, the `EXCLUDE` constraint's `WHERE (status <> 'cancelled')` clause already blocks any other overlapping booking for that property. `PENDING` holds the dates exactly as firmly as `CONFIRMED` does -- there's no separate "provisional hold" mechanism, the status itself is the hold.
3. **Guest pays** -- not built yet (Epic 8 / `BOOK-10`). A separate payment step happens against this pending reservation.
4. **Payment succeeding is what triggers confirmation** -- the (future) payment webhook calls `transition_to(reservation, CONFIRMED)` internally. The guest never directly "confirms" anything themselves; confirmation is a side effect of payment succeeding, not a step the guest performs. See [`state-machine-basics.md`](state-machine-basics.md#4-confirm_reservation--a-placeholder-for-a-trigger-that-doesnt-exist-yet) for why this is easy to get backwards.
5. **If payment fails instead** -- the saga's compensating action cancels the reservation (`transition_to(reservation, CANCELLED)`), freeing the dates.
6. **If nobody ever pays** -- Epic 6's (not-yet-built) expiry job clears abandoned `PENDING` holds once `expires_at` passes. Nothing enforces this today; `expires_at` is set on every reservation now so that job has a real value to filter on once it exists.
7. **After checkout** -- Epic 6's completion job moves `CONFIRMED -> COMPLETED` once the checkout date has passed.
8. **Cancellation** (`BOOK-38`, done) -- the guest can cancel their own `PENDING` or `CONFIRMED` reservation any time before `COMPLETED`, via `POST /reservations/{id}/cancel`.

## What exists today vs. what's stubbed

| Step | Real trigger (eventual) | What exists right now |
|---|---|---|
| 1-2: create + hold | Guest submits the form | Fully built -- `create_reservation`/`create_reservation_idempotent` |
| 3-4: pay -> confirm | Payment webhook, on success | Stubbed: `POST /reservations/{id}/confirm` skips straight to "payment succeeded," so the transition itself is testable before Epic 8 exists |
| 5: pay fails -> cancel | Payment webhook, on failure | Not built -- same `transition_to(..., CANCELLED)` call `BOOK-38`'s endpoint already uses, just a different caller |
| 6: abandoned hold expires | Epic 6's background job | Not built -- `expires_at` is already populated, waiting for a job to read it |
| 7: checkout passes -> complete | Epic 6's background job | Not built -- the transition rule exists in `_LEGAL_TRANSITIONS`, no trigger yet |
| 8: guest cancels | Guest, any time before `COMPLETED` | Fully built -- `BOOK-38` |

The pattern repeats: every future trigger (webhook, background job) is expected to call the *same* `transition_to()` that already exists, just from a different caller. `BOOK-37`'s whole point was making sure the rule itself never has to change when the trigger does.

## Why a `PENDING` reservation already blocks the calendar, not just `CONFIRMED`

This is worth calling out because it's the one part of the flow that's easy to get backwards: it might seem safer to only block dates once payment is confirmed, and let `PENDING` reservations be "soft" (not really holding anything). That's not how this is built, deliberately -- if `PENDING` didn't block, two guests could both get as far as "reservation created, please pay" for the same dates, and only one payment could actually complete correctly, with the loser finding out only after they'd gone through the payment flow. Blocking from `PENDING` means the *first* guest to successfully create the reservation row is the only one who ever gets to the payment step for those dates -- the `EXCLUDE` constraint decides that the instant the row is inserted (`BOOK-35`), not later at payment time. The cost is the flip side: an abandoned `PENDING` reservation genuinely blocks those dates for everyone else until it expires -- which is exactly why `expires_at`/Epic 6's expiry job matter, not as a nice-to-have but as the other half of this design actually working.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `PENDING` holds dates | The `EXCLUDE` constraint blocks overlaps for any non-`cancelled` status -- `PENDING` is a real hold, not a soft one |
| Payment -> confirm, not confirm -> payment | Confirmation is a side effect of payment succeeding; the guest never triggers confirm directly |
| `POST /reservations/{id}/confirm` | A manual stand-in for Epic 8's webhook -- calls the same `transition_to()` the real trigger will |
| `expires_at` | Set on every reservation today, enforced by nobody yet -- waiting for Epic 6's expiry job |
| Every future trigger reuses `transition_to()` | Webhooks and background jobs call the same function `BOOK-37`/`38`'s endpoints already call -- only the caller changes, never the rule |
