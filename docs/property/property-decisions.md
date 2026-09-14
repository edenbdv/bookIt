# What BookIt chose for the property/host side, and why

Same shape as `docs/payment/payment-decisions.md`: what was picked, what was rejected,
and when to revisit.

## 1. BOOK-102: a minimal host UI, and the backend gaps it exposed

**The starting problem:** the frontend had zero host-facing surface — `Feed`,
`PropertyDetail`, `Login`, `Signup` are all guest browsing/booking. A host account had
nowhere to see or manage anything as a host, even though most of the backend already
existed (`POST/PATCH/DELETE /properties/{id}`, photo endpoints, and
`POST /reservations/{id}/reviews` already inferring the author as guest *or* host).
What was actually missing wasn't UI alone — there was no way to even *query* "my
properties" or "reservations on my property" at all.

### 1.1 `host_id` as a plain filter on the existing public listing endpoint

**Chosen:** `GET /properties?host_id={id}` — one more optional filter on
`list_filtered()`, same as `city`/`guests`/price range. No new endpoint, no auth
required: which properties a host owns is public information already visible by
browsing, same as filtering by city.

### 1.2 A property's reservations are a reservation-module endpoint, not a property-module one — even though the URL says `/properties/...`

**Chosen:** `GET /properties/{property_id}/reservations` lives in
`reservation/router.py`, which gave up its `/reservations` prefix and now writes each
route's full path explicitly. This mirrors `review/router.py`'s existing, deliberate
choice (its own comment: *"URL nesting doesn't decide module ownership, return type
does"*) — that file already owns endpoints under both `/reservations/...` and
`/properties/...` for the same reason. Extended an existing precedent rather than
inventing a second convention.

**What this doesn't change:** which module owns which table's invariants. Reservation
still exclusively owns `Reservation`'s state machine; property still exclusively owns
`Property`. `reservation/service.py` reaching into `uow.properties.get_active_by_id()`
isn't new coupling either — `create_reservation()` already did that, to check
self-booking and `guest_count` against `max_guests`.

**Auth shape:** host-only, not public — unlike the `host_id` filter above, this
returns guest identities and booking details. `current_user` (token) says *who's
asking*; `property_id` (URL) says *which property's bookings*. A host can own more than
one property, so the token alone can't answer "which one" — both are needed, for
different jobs.

### 1.3 `GET /auth/me` — the missing "who am I", instead of decoding the JWT client-side

**The gap:** the frontend's `Session` never carried the logged-in user's own id —
needed to actually call the `host_id` filter above. The access token's `sub` claim
technically already has it.

**Chosen:** a real endpoint (`UserRead`, reusing the existing `get_current_user`
dependency) over decoding the token client-side. An access token's job is
authorization, not identity delivery — the same reason OAuth splits `access_token` from
`id_token` rather than overloading one. Decoding it in the frontend would also only
ever yield the bare id (`sub`)/session id (`sid`); anything else (`display_name`, etc.)
would still need a real fetch. And it isn't scoped to this one ticket — nothing in the
frontend could answer "who's logged in" before this, so the next feature that needs it
would have hit the same wall.

### 1.4 Deleting a property doesn't check for open reservations — until this ticket

**The bug, found by asking "what if a host deletes a property with a future booking?":**
`delete_property()` only ever set `deleted_at`, with no check against `Reservation` at
all. A guest with a `CONFIRMED`, paid, future stay kept a completely untouched
reservation — but the property now 404s (`get_active_by_id` excludes delisted rows) and
disappears from search. The reservation itself stayed valid (cancel/refund still worked,
since `cancellation.py` looks the property up via the unfiltered `get_by_id`), but
became invisible: there's still no "my bookings" list for guests (BOOK-85/BOOK-98), so
there was no page anywhere that would show it to them.

**Chosen:** `ReservationRepository.has_active_by_property_id()` — `PENDING` or
`CONFIRMED` (the two non-terminal statuses, same set `_LEGAL_TRANSITIONS` already
encodes) blocks the delete with a `409`. `CANCELLED`/`COMPLETED` don't block it — nothing
left to disrupt.

**Ordering:** ownership (`403`) is still checked before this (`409`) — same "existence
and ownership outrank everything else" order `update_property`/`delete_property` already
used; a non-owner has no business learning whether the property has active bookings.

### 1.5 Property photos stay URL-only; no upload was built

**Chosen:** the add-photo form takes a pasted URL (`PropertyPhotoCreate.url: str`),
same contract the seed data and `PhotoCarousel` already assumed. A failed image load
now shows a visible "Couldn't load" placeholder with the offending URL instead of
silently disappearing — cheap, and it turns a silent failure into a diagnosable one.

**Why real upload wasn't built:** the honest version isn't "add a file input" — it's
direct-to-storage upload via pre-signed URLs (client uploads straight to S3, bypassing
the app server for the bytes), a CDN in front of storage, and an async thumbnailing/
EXIF-stripping/content-sniffing pipeline. That's meaningfully more infrastructure than
this ticket's scope, and most of it (CDN, malware scanning) is infrastructure a real
team buys rather than builds. Local-filesystem storage was considered and rejected for
now too: it would work today (the backend runs directly via `uvicorn`, not
containerized, so disk persistence isn't the blocker), but it's a dead end that doesn't
resemble the real pattern, so it wasn't worth building either.

**Revisit when:** if this ever becomes a learning exercise in its own right, the
pre-signed-URL flow (1.5's "chosen" paragraph) is the one piece worth actually building;
the rest is reasonably left as "this is what real apps do," not re-implemented.

### 1.6 What this unblocks

BOOK-80 (host-initiated cancellation) was deprioritized earlier specifically because
there was no host UI to trigger it from — that blocker is gone now (see §2). BOOK-98/
BOOK-85 (a live-updating payment UI, and `GET /reservations/{id}`) remain untouched;
this ticket's `GET /properties/{id}/reservations` answers "reservations for one
property", not "one reservation by id" or "my reservations as a guest" — a different,
still-open gap.

## 2. BOOK-80: host-initiated cancellation is a distinct endpoint, always a full refund

**Chosen:** `POST /reservations/{id}/host-cancel`, a separate endpoint from the
guest-only `POST /reservations/{id}/cancel` — not the same endpoint with a broadened
auth check. The two have genuinely different consequences (refund rule, caller,
eventual notification), so branching one endpoint on "who's calling" would hide two
different actions behind one code path. `reservation/service.py`'s
`host_cancel_reservation()` checks `property.host_id`, not `reservation.guest_id`, but
shares the same `cancel_reservation_system()` transition underneath — so it inherits the
same state machine for free: legal from `PENDING`/`CONFIRMED`, a `409` from
`CANCELLED`/`COMPLETED`.

**Refund rule:** always the full charged amount, never `calculate_refund_cents()`'s
policy-based partial — a guest shouldn't eat a cancellation penalty for the host's
decision. Concretely: `PENDING` (never charged) refunds `0`, and `CONFIRMED` refunds the
full charge even same-day or past check-in, where a guest-initiated cancel would return
`0` under the property's policy. Verified live against real seeded data, both cases.

**Deliberately deferred:** a penalty/strike system for hosts (no such concept exists in
the data model at all — a real feature on its own, not a small addition here), and
host-cancel-specific notification copy (`send_reservation_cancelled_email`'s own
docstring already stays neutral on purpose, "fires for both an explicit cancel and a
silent auto-expiry" — host-cancel is just a third source that same neutral wording
already covers, for free, today).

**Testing gap, inherited, not introduced here:** `host_cancel_reservation_and_refund()`
(the payment-orchestration layer) has no unit test, same as its guest-side sibling
`cancel_reservation_and_refund` never had one — both call `service.create_refund()`,
which has no `force=` override and always calls the real mock provider with its random
weighted outcome and unconditional simulated latency. Verified live instead (three
authorization/state cases, plus both refund cases above), matching how the guest-side
version was verified when it shipped. `reservation/service.py`'s own
`host_cancel_reservation()` (the authorization + transition layer, no payment call) is
fully unit-tested — 5 cases, deterministic, no provider involved.

**Frontend:** a "Cancel reservation" action on `PENDING`/`CONFIRMED` rows in the host's
bookings list (`PropertyReservations.tsx`), inline-confirmed the same way as
`HostProperties`' delete-property flow — no browser `confirm()`/`alert()`. Once it
returns, the row's status badge updates in place and shows the refund outcome
("Refunded $X in full" or "No refund — never charged"), matching `BookingForm`'s own
guest-side cancel messaging.

## 3. BOOK-85: read endpoints for reservations — two, not one, because they answer
different questions

**The gap:** `reservation/router.py` was write-only — `POST` create/confirm/cancel/
host-cancel, but no `GET` at all. A guest could book, leave the tab, and never see that
reservation again — it sits in the database exactly right, but the frontend has no way
to ever ask for it back. Framed as more than a UX gap: it's the missing safety net for
the `TIMEOUT`/`PENDING` resolution window BOOK-55/100/101 exist for — without a read
path, nothing shows a guest whether their payment ever resolved once they leave
checkout.

**Chosen: two endpoints, not one shared endpoint, because they answer different
questions.**
* `GET /reservations` — "everything I've booked." Guest-only by construction
  (`WHERE guest_id = current_user.id`) — no host branch to write at all.
* `GET /reservations/{id}` — "this one reservation's state." Visible to either its own
  guest **or** the property's host — the same "participant" idea `review/service.py`'s
  `NotAParticipantError` already uses for reviews going both directions. One endpoint
  here, unlike BOOK-80's split cancel actions, because this is a pure read: guest and
  host see the identical data, nothing branches on who's asking beyond "are you allowed
  to see this at all." BOOK-80 split because its two callers had different *effects*
  (different refund rule, different mutation) — a read has no effects to differ.

**Not filtered:** no status filter, no hiding an expired-but-not-yet-swept `PENDING`
row (`BOOK-46`'s expiry job runs on its own schedule, so that window is real). Hiding it
would hide real state from someone entitled to see it — same reasoning `TIMEOUT` was
never collapsed into `FAILURE` elsewhere in this project.

**Deliberately deferred:** a host-side aggregate across *every* property a host owns —
BOOK-102 already gives a per-property view (`GET /properties/{id}/reservations`); a
combined cross-property one is a separate, easy-to-add-later endpoint if actually
wanted, not part of this ticket's original ask.

**Frontend, expanded past the original "static list" plan:** partway through, it was
clear a read-only list wasn't enough — a guest looking at a stuck `PENDING` booking had
no way to actually act on it from this page. Those actions already existed
(`payReservation`/`cancelReservation`), just reachable only from the exact tab that
created the booking, via `BookingForm`. Added "Pay & confirm" and "Cancel" to each
`PENDING`/`CONFIRMED` row on `MyBookings.tsx`, reusing that same status-derivation
logic. `Spinner` was pulled out of `BookingForm.tsx` into its own component since it's
now shared by both.

**Still explicitly out of scope:** the list is a snapshot as of page load — a `PENDING`
row doesn't update on its own if the payment resolves while the tab stays open. That's
BOOK-98, which this ticket unblocks but doesn't implement.

**Testing:** `get_reservation()`'s four cases (guest, host, unrelated third party,
nonexistent id) are unit-tested. `list_reservations_for_guest()` is a thin,
zero-branching delegation to the repository — not separately unit-tested, same
precedent as `property/service.py`'s equally thin `list_cities()`. Both endpoints and
the new pay/cancel actions were additionally verified live: real seeded accounts for
the authorization matrix, a freshly created unpaid reservation to confirm `PENDING`
actually surfaces and pays through to `CONFIRMED`, then cancels with the correct refund
message.

## 4. BOOK-98: poll `GET /reservations/{id}` until a PENDING payment resolves

**The gap this closes:** `BookingForm` and `MyBookings` both showed a one-off "still
processing" message on a `pending` payment, then reset to idle with no way to find out
what happened next short of clicking "Pay & confirm" again. Once BOOK-55's webhook (or
BOOK-101's reconciliation worker) resolves it asynchronously, nothing told the browser —
the backend discovering the truth and the guest's screen reflecting it are two separate
problems, and this ticket is the second one.

**Chosen: poll, not push** — already decided in `webhook-basics.md` §3, restated here
because this is where it's actually implemented: a single guest watching their own
booking doesn't need instant multi-client updates, so a WebSocket/SSE channel would be
solving a problem this doesn't have.

**What's polled:** `GET /reservations/{id}` (BOOK-85) — no new payment-status endpoint.
Once the saga resolves the payment, `reservation.status` itself flips from `pending` to
`confirmed`/`cancelled`, so the reservation is already the signal.

**Shared, not duplicated:** `utils/pollReservation.ts`'s `pollUntilResolved()` — a
2.5s interval, capped at ~50s total (~20 attempts) before giving up and telling the
guest to check back later. The cap exists because, per `payment-decisions.md` §8, the
webhook is manual-trigger-only in dev — polling with no cap would leave a guest staring
at a spinner forever if nothing ever resolves it locally. Used by both `BookingForm`
(the just-booked flow) and `MyBookings` (the revisit-later flow), which had the
identical gap.

**Extra, found while implementing, not part of the original ask:** retrying
`payReservation()` on a reservation whose earlier attempt already timed out hits
BOOK-100's `PendingChargeExistsError` guard — a `409`. That `409` was previously a
dead-end error message, even though it literally means "a payment is already resolving
for this one" — exactly the case polling exists for. Both `confirm()` and `handlePay()`
now catch a `409` specifically and fall back to the same `pollAndSettle()` helper,
rather than starting a poll only on a fresh `'pending'` response. This is the realistic
path for a guest who closed the tab mid-`TIMEOUT` and came back later — the *original*
payment attempt already returned, so there's no fresh `'pending'` response to trigger
polling from; the retry's `409` is the only signal available.

**A real StrictMode bug, caught by testing this live rather than trusting the code
read:** both `unmountedRef` guards were being set to `true` immediately on mount, before
any poll ever ran — React 18 `StrictMode`'s dev-only double-invoke (mount → cleanup →
mount) runs the effect's cleanup once as part of that simulation, and a ref that's
*only* ever set `true` in cleanup comes out of that already poisoned, even though the
component is genuinely still mounted. Every poll's very first `isCancelled()` check
returned `true` and aborted before making a single request — confirmed by watching the
network log stay empty across three separate real attempts before tracing it to this.
Fixed by resetting the ref to `false` at the top of the effect, not just setting it in
the cleanup.

**Verified live, end to end:** a script created a genuinely stuck `PENDING` payment
(`force=TIMEOUT`, bypassing the mock's normal 2% odds) tied to a real reservation. In
the browser: clicking "Pay & confirm" produced the expected `409`, the persistent
"still confirming" state appeared, and the poll's `GET /reservations/{id}` showed up in
the network log. Running `scripts/simulate_provider_webhook.py <payment_id> success`
from a terminal resolved it, and the row flipped from `PENDING` to `CONFIRMED` on its
own within one poll tick — no reload.

**Not live-tested:** the ~50s "gave up, still pending" path — would need an actual 50s
wait to observe; the logic itself is a plain, fixed attempt-count cap with nothing
non-deterministic in it, so this was left to code review rather than a real-time wait.
