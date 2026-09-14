# Reservation lifecycle, end to end

Ties together `POST /reservations` (`BOOK-34`/`35`/`36`), the transition table (`BOOK-37`), cancellation (`BOOK-38`), background jobs (Epic 6), and events (Epic 7) into one picture -- what actually happens to a `Reservation` row from creation to its final state, and which parts exist today vs. are still stubbed out for a later epic.

**Prerequisite:** [`state-machine-basics.md`](state-machine-basics.md) (the transition table itself), [`../db/exclude-constraint-basics.md`](../db/exclude-constraint-basics.md) (why `PENDING` already blocks overlapping dates), [`../db/idempotency-basics.md`](../db/idempotency-basics.md) (the `Idempotency-Key` header on step 1), [`../jobs/worker-decisions.md`](../jobs/worker-decisions.md) (why the notification steps below are split between a direct Celery call and Kafka).

## The flow

1. **Guest requests a booking** -- `POST /reservations`. A `Reservation` row is created immediately, status `PENDING`, with `expires_at` set `PENDING_HOLD_MINUTES` (15) out. This also enqueues the "reservation received" email (`BOOK-45`, done) -- fires once the transaction actually commits (`UnitOfWork.on_commit()`), regardless of what happens to the reservation afterward.
2. **The dates are locked in from that instant.** Even though it's only `PENDING`, the `EXCLUDE` constraint's `WHERE (status <> 'cancelled')` clause already blocks any other overlapping booking for that property. `PENDING` holds the dates exactly as firmly as `CONFIRMED` does -- there's no separate "provisional hold" mechanism, the status itself is the hold.
3. **Guest pays** -- not built yet (Epic 8 / `BOOK-10`). A separate payment step happens against this pending reservation.
4. **Payment succeeding is what triggers confirmation** -- the (future) payment webhook calls `transition_to(reservation, CONFIRMED)` internally. The guest never directly "confirms" anything themselves; confirmation is a side effect of payment succeeding, not a step the guest performs. See [`state-machine-basics.md`](state-machine-basics.md#4-confirm_reservation--a-placeholder-for-a-trigger-that-doesnt-exist-yet) for why this is easy to get backwards. Once this transition lands, a `BookingConfirmed` event is meant to fire from inside it (Epic 7, `BOOK-49`, in progress) -- not built yet, but the hook point is this exact function call, not the endpoint around it.
5. **If payment fails instead** -- the saga's compensating action cancels the reservation (`transition_to(reservation, CANCELLED)`), freeing the dates. Same event mechanism as step 4, `BookingCancelled` this time -- one saga failure path, no separate notification code needed once Epic 7 lands, because it reuses the same `cancel_reservation()` step 8 already calls.
6. **If nobody ever pays** -- Epic 6's expiry job (`BOOK-46`, done) clears abandoned `PENDING` holds once `expires_at` passes, running every 60 seconds via Celery beat. This is also a `CANCELLED` transition -- and per `BOOK-49`'s scope, it emits the *same* `BookingCancelled` event step 5 does. A guest whose hold silently expired gets the same "cancelled" notification as one who was told "payment failed" -- deliberately, so the auto-expiry path isn't silently unnotified just because nobody clicked anything.
7. **After checkout** -- a completion job moves `CONFIRMED -> COMPLETED` once `check_out` has passed (treated as midnight UTC of that date, same convention `BOOK-76`'s refund calculation uses), running hourly via Celery beat. Unblocks `BOOK-86` (write-a-review UI), which requires `COMPLETED` and had no way to reach it before this. No event fires on this transition -- unlike steps 4-6, nothing consumes a "completed" notification yet.
8. **Cancellation** (`BOOK-38`, done) -- the guest can cancel their own `PENDING` or `CONFIRMED` reservation any time before `COMPLETED`, via `POST /reservations/{id}/cancel`. Same `BookingCancelled` event as steps 5/6, once Epic 7 lands.

## The full picture: creation, payment, background jobs, and notifications together

```
Guest books                                                    (Epic 4, done)
    │
    ▼
POST /reservations → status PENDING
    │
    ├─ on_commit() → Celery .delay() → "received" email        (Epic 6, done --
    │                                    (direct, permanent,      never touches Kafka)
    │                                     never moves to Kafka)
    │
    ▼
 ... time passes ...
    │
    ├── nothing happens ──► BOOK-46's job cancels it (expired)  (Epic 6, done)
    │                              │
    └── something calls confirm_reservation() ──────────────────┐
                                   │                              │
              ┌────────────────────┴────────────────────┐        │
              │                                          │        │
     TODAY: the manual stub                    AFTER EPIC 8: a real
     endpoint, no payment behind it            payment webhook calls it,
     (anyone can call it right now)            only on actual payment success
              │                                          │
              └────────────────────┬────────────────────┘
                                   ▼
                    confirm_reservation() runs -- SAME function,
                    regardless of who called it
                                   │
                                   ├─ status → CONFIRMED
                                   └─ writes BookingConfirmed to outbox   (Epic 7, BOOK-49)
                                              │
                                              ▼
                                   relay → Kafka → Notification consumes  (Epic 7, BOOK-48/50/51)
                                              │
                                              ▼
                                   "confirmed" email sent
```

**The point of drawing it this way:** Epic 7 never touches *why* a reservation gets confirmed or cancelled -- only *what happens once it does*, by hooking into `transition_to()`'s callers (`BOOK-49`), not the HTTP layer above them. Epic 8 is entirely about replacing *who calls* `confirm_reservation()` -- swapping the manual stub for a real payment webhook -- and gets the whole notification pipeline for free, because it's calling the same function Epic 7 already instrumented. Same pattern as the `expires_at`/expiry-job history below: the rule (`transition_to`) never changes: only the caller does, and now also the side effects wired onto it.

## What exists today vs. what's stubbed

| Step | Real trigger (eventual) | What exists right now |
|---|---|---|
| 1-2: create + hold | Guest submits the form | Fully built -- `create_reservation`/`create_reservation_idempotent` |
| 1: "received" email | Fires on commit, every time | Fully built -- `BOOK-45`, direct Celery call, permanent (never migrates to Kafka -- `worker-decisions.md` §9) |
| 3-4: pay -> confirm | Payment webhook, on success | Stubbed: `POST /reservations/{id}/confirm` skips straight to "payment succeeded," so the transition itself is testable before Epic 8 exists |
| 4/5/6: "confirmed"/"cancelled" emails | Any call to `confirm_reservation()`/`cancel_reservation()`/the expiry job | Not built -- Epic 7 (`BOOK-48`-`51`), in progress. Outbox table (`BOOK-50`) and the event-writing hook (`BOOK-49`) come first; Kafka infra (`BOOK-48`) and the Notification consumer (`BOOK-51`) after |
| 5: pay fails -> cancel | Payment webhook, on failure | Not built -- same `transition_to(..., CANCELLED)` call `BOOK-38`'s endpoint already uses, just a different caller |
| 6: abandoned hold expires | Epic 6's background job | Fully built -- `BOOK-46`, runs every 60s via Celery beat |
| 7: checkout passes -> complete | Background job | Fully built -- runs hourly via Celery beat, unblocks `BOOK-86` |
| 8: guest cancels | Guest, any time before `COMPLETED` | Fully built -- `BOOK-38` |

The pattern repeats, and now applies to notifications too: every future trigger (webhook, background job) calls the *same* `transition_to()` that already exists, and once Epic 7 lands, that same call is also what fires the right event -- `BOOK-37`'s whole point (the rule never changes when the trigger does) turned out to extend cleanly to "and neither does the notification," once the event hook lives inside the function instead of at each caller.

## Why a `PENDING` reservation already blocks the calendar, not just `CONFIRMED`

This is worth calling out because it's the one part of the flow that's easy to get backwards: it might seem safer to only block dates once payment is confirmed, and let `PENDING` reservations be "soft" (not really holding anything). That's not how this is built, deliberately -- if `PENDING` didn't block, two guests could both get as far as "reservation created, please pay" for the same dates, and only one payment could actually complete correctly, with the loser finding out only after they'd gone through the payment flow. Blocking from `PENDING` means the *first* guest to successfully create the reservation row is the only one who ever gets to the payment step for those dates -- the `EXCLUDE` constraint decides that the instant the row is inserted (`BOOK-35`), not later at payment time. The cost is the flip side: an abandoned `PENDING` reservation genuinely blocks those dates for everyone else until it expires -- which is exactly why `expires_at`/Epic 6's expiry job matter, not as a nice-to-have but as the other half of this design actually working.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `PENDING` holds dates | The `EXCLUDE` constraint blocks overlaps for any non-`cancelled` status -- `PENDING` is a real hold, not a soft one |
| Payment -> confirm, not confirm -> payment | Confirmation is a side effect of payment succeeding; the guest never triggers confirm directly |
| `POST /reservations/{id}/confirm` | A manual stand-in for Epic 8's webhook -- calls the same `transition_to()` the real trigger will |
| `expires_at` | Set on every reservation, enforced by Epic 6's job (`BOOK-46`, done) every 60 seconds |
| `check_out` -> `COMPLETED` | Enforced by a background job, hourly -- unblocks `BOOK-86`'s review flow |
| Every future trigger reuses `transition_to()` | Webhooks and background jobs call the same function `BOOK-37`/`38`'s endpoints already call -- only the caller changes, never the rule |
| Events hook the function, not the endpoint | Epic 7's `BookingConfirmed`/`BookingCancelled` fire from inside `confirm_reservation()`/`cancel_reservation()` themselves -- so Epic 8's real payment trigger inherits the whole notification pipeline for free, without building anything new for it |
