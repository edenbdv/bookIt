# What BookIt chose for payments, and why

The payment-domain companion to `docs/jobs/worker-decisions.md` — same shape: what was
picked, what was rejected, and when to revisit. See `docs/domain-layer/reservation-lifecycle.md`
for how this epic plugs into the reservation state machine Epic 4 already built —
Epic 8 replaces *who calls* `confirm_reservation()`/`cancel_reservation()`, not the
functions themselves.

## 1. BOOK-52: the mock provider's outcome is weighted-random by default, force-able for callers that need determinism

**Chosen:** `services/payment/src/payment/provider.py`'s `charge(amount_cents, *, force=None)` returns
`SUCCESS`/`FAILURE`/`TIMEOUT`. Unforced, the outcome is random but weighted toward
success (90/8/2) — not a 50/50 coin flip — so exercising the API by hand mostly behaves
like a real integration, with the failure/timeout paths still reachable without waiting
for them. `force=` lets a caller (BOOK-53/54's saga, and their tests) pin an exact
outcome deterministically.

**Why not base the outcome on `amount_cents` instead** (a "magic amount" scheme, closer
to how some real sandboxes work): would have tied outcome control to a value that
already means something real (what the guest pays) — a test wanting to force a failure
would have to pick a specific reservation price to get one, coupling two unrelated
concerns. `force` keeps "what should the mock do" an explicit, separate input.

**TIMEOUT is not FAILURE, and BOOK-53/54 need to keep that distinction:** `FAILURE`
means the provider told us the charge didn't happen. `TIMEOUT` means we don't know —
the charge may have actually succeeded on the provider's side, with the response lost
in transit. Treating a timeout as a confirmed failure and blindly retrying risks a
double charge. This ticket only introduces the distinct outcome; making the retry path
actually safe under it is BOOK-53's idempotency job, not resolved here.

**Revisit when:** BOOK-53's endpoint needs the mock to simulate real latency (an
actually-slow call, not just an immediate `TIMEOUT` return value) — nothing here blocks
today, which is deliberate for fast, deterministic tests, but a future retry/backoff
ticket might need to exercise real timing.

## 2. BOOK-53: `IdempotencyKeyStatus.COMPLETED` means the request attempt is done, not that the payment succeeded

**The trap this section exists to head off:** `key_row.status = IdempotencyKeyStatus.COMPLETED`
is set unconditionally after `create_payment()` returns — including when the outcome was
`TIMEOUT` and `Payment.status` is still `PENDING`. Reading `COMPLETED` as "it worked" is
natural and wrong. `IdempotencyKeyStatus` answers *"did we finish attempting this
request?"*; `PaymentStatus` answers *"what happened to the money?"* — two different
questions, two different columns, on two different tables.

**Why marking it `COMPLETED` on a timeout is correct, not a bug:** a retry with the same
key must never trigger a second call to the provider for an unresolved payment — that's
exactly the double-charge risk §1's TIMEOUT/FAILURE split exists to avoid. Marking the
key `COMPLETED` (with a still-`PENDING` payment attached) means a retry hits
`create_payment_idempotent()`'s `existing.status == COMPLETED` branch and simply returns
the same `PENDING` payment again — no new charge attempt, no new provider call. Waiting
for BOOK-55's webhook to resolve the ambiguity is the only thing allowed to change that
payment's status from here.

## 3. `IdempotencyKey.expires_at` gets a cleanup job now, deferred since BOOK-36

**The gap found while building BOOK-53:** `expires_at` was set on every key from BOOK-36
onward, but nothing ever read it back — a request handler only checks `status`, never
whether the row has "expired." Left alone, a key stays authoritative forever; the column
only ever bounded table growth, never request behavior. That growth-only purpose was
already deferred once (`reservation/models.py`'s original comment said so); moving the
model to `core/idempotency/` for BOOK-53 was the natural point to stop deferring it.

**Chosen:** `core/idempotency/cleanup.py`'s `delete_expired_keys()` — same shape as
BOOK-50's `core/outbox/cleanup.py`, a daily Celery beat task (`crontab(hour=3, minute=30)`,
right after the outbox cleanup's `3:00`) deleting rows where `expires_at` has passed.
Deleting, not archiving — once a key is past its retry window, there's nothing left
worth keeping it for (unlike the outbox's published rows, which stick around briefly for
debugging).

**Revisit when:** a real need for auditing expired idempotency attempts shows up —
nothing here today, so deletion over archival is the simpler default until one does.

## 4. `PaymentIdempotencyKeyMismatchError`: a reused key must match the original request

**Checked live against the actual frontend, not assumed:** `frontend/src/components/BookingForm.tsx`
already generates a fresh `crypto.randomUUID()` inside the submit handler on every
submission, so this gap was never reachable through BookIt's own client — this is
defense-in-depth for any caller, not a live bug fix.

**Chosen:** same-key-different-body is rejected explicitly (`400`), not silently
resolved either direction — full reasoning in `docs/db/idempotency-basics.md`'s
"Scoping the key to its owner" section. Applied to both `payment/service.py` and
`reservation/service.py`'s idempotent-create flows for consistency, since the
underlying gap was identical in both (BOOK-36 predates BOOK-53, and had it too).

## 5. BOOK-54: why this needs a saga at all, given BookIt is one monolith on one database

**General pattern, no BookIt specifics: `saga-basics.md`.** This section is the
project-specific decision; that doc covers why "distributed system" is the wrong
mental trigger for needing a saga, and what actually is.

**The question worth answering directly, since it's a fair one: BookIt is a monolith,
`reservations` and `payments` are tables in the same Postgres database — so why not
just wrap "charge" and "confirm" in one transaction?** Because the step in between isn't
a database operation. Calling the payment provider (real or `provider.py`'s mock,
standing in for one) is a call to another system, for the identical reason a Kafka
publish is (`outbox-basics.md` §2) — it can't be inside our transaction no matter how
many or few services BookIt has. That's the actual trigger for needing saga treatment
here, not service count.

**Third instance of the same underlying problem this project keeps hitting, with a
third kind of consequence:**

| | The non-transactional step | What closes the gap | Consequence if unhandled |
|---|---|---|---|
| Outbox (BOOK-50) | Publishing to Kafka | Write intent in the DB transaction, publish separately | A lost event |
| Inbox (BOOK-51) | Sending an email | Check-then-act-then-mark | A duplicate email |
| Saga (BOOK-54) | Charging the payment provider | Forward step + compensating step | Money moved, our state disagrees |

**Why payment needs a compensating action and BOOK-45's email never did (`saga-basics.md`
§3's two questions, applied):** the reservation's next state (confirmed or cancelled)
*is* a direct function of the payment outcome — a real decision point, something
downstream genuinely branching on it. And a *successful* charge leaves a real external
effect (money moved) that a stuck-partway process would need to account for. Email
fails both tests — nothing branches on whether it sent, and a sent email has no effect
worth undoing — which is exactly why `worker-decisions.md` §9 could accept "duplicates
are fine" there instead of building any of this.

**Chosen: orchestration, not choreography (`saga-basics.md` §4).** One function
(`services/payment/src/payment/saga.py`) explicitly calls the charge step, then explicitly decides
confirm vs. compensate — not each side reacting to a `BookingConfirmed`/`PaymentFailed`
event over Kafka. Same reasoning `worker-decisions.md` §9 already used for BOOK-45's
trigger: a single, tight, one-consumer flow doesn't earn the decoupling cost of routing
through the event bus just because BOOK-48–51 already built one.

**Two concrete reuse traps found while designing this, both from rereading the
functions the saga needs to call, not guessing:**

1. **`confirm_reservation()` has no ownership check at all** — safe to call directly
   from the saga, unchanged. It's already written for exactly this: its own comment
   calls it "a stand-in for Epic 8's real trigger."
2. **`cancel_reservation()` is the wrong function for the compensating action** — it
   requires `current_user_id == reservation.guest_id`, because it's built for BOOK-38's
   guest-initiated HTTP cancel. The saga's compensation is a system action, not a guest
   action. `expire_pending_reservations()` (BOOK-46) already solved this identical
   problem by calling `transition_to()` + `_emit_outbox_event()` directly instead of
   going through `cancel_reservation()` — the saga should use that same shape, ideally
   via a shared internal helper both it and the expiry job call, rather than a third
   copy of the same two lines.

**The saga must be idempotent-safe against BOOK-53's own idempotent endpoint
(`saga-basics.md` §5) — two separate operations, two separate guards.**
`create_payment_idempotent()` only guarantees "don't create a payment twice"; it says
nothing about how many times *the caller* then acts on the payment it returns. A retry
returns the same already-resolved `Payment` — if the saga reacted to it
unconditionally, that retry would call `confirm_reservation()` a second time on an
already-`CONFIRMED` reservation, which `transition_to()` rejects as an illegal
transition (raises, doesn't no-op). The saga checks the reservation's current status
before acting: no longer `PENDING` means already reacted to, so treat it as a no-op
rather than letting the state machine's own rejection surface as an unhandled error.

## 6. Split the commit: the charge must outlive a failure in the reaction step

**Found by asking "how is this a saga if there's no compensation for a successful
charge?"** — a fair challenge. The first version had `create_payment_idempotent()` and
`react_to_payment_outcome()` sharing one open transaction, committed once at the very
end. `charge()` is an external, irreversible effect the same way a Kafka publish is
(`outbox-basics.md` §2) — if `confirm_reservation()` then raised for any reason before
that single commit, the *entire* transaction rolled back, including the record that the
charge succeeded. The real charge would still stand at the provider; our own database
would have no memory of it. Worse: since a rolled-back attempt leaves no row at all
(`idempotency-basics.md` §5's same logic), a retry with the same key would call
`charge()` again from scratch — a genuine double charge, with nothing recorded to even
notice.

**Chosen:** `services/payment/src/payment/saga.py`'s `create_payment_and_react()` commits right after
`create_payment_idempotent()` returns, *before* calling `react_to_payment_outcome()`.
Two local transactions, not one — matching `saga-basics.md` §2's own diagram, where
step 1 commits before the external-call-dependent step ever runs. A failure in
confirming/compensating afterward now rolls back only its own transaction; the payment
record survives.

**Confirmed live, not just reasoned about:** `scripts/verify_saga_commit_split.py`
forces a successful charge, then makes the reaction step raise immediately afterward
(simulating a crash) — the `Payment` row is still there as `SUCCEEDED` afterward, and
the reservation correctly never got confirmed.

**What this fix does *not* close, found immediately after fixing it — a real,
still-open gap:** the process crashing **between `charge()` returning and that first
commit actually landing**. No local commit ordering can close that window; it's the
irreducible core of the distributed-transaction problem (`saga-basics.md` §6) — two
systems, no shared transaction, and the instant the external system's state changes
isn't under our control at all.

**Deliberately not built now, and why:**
- **Provider-side idempotency keys** (sending our own key *to* the provider, so a
  crash-retry doesn't double-charge even there) — `provider.py` is a mock with nothing
  to be idempotent *against*; there's no real external state to protect yet.
- **Reconciliation** — a job comparing our records against the provider's own list of
  what happened — has nothing to reconcile against for the same reason.

**What actually does close most of it, already on the roadmap:** BOOK-55's webhook.
Worth being explicit about this, since it's easy to under-scope BOOK-55 as "just handles
`TIMEOUT`" — a webhook is the provider's own, independent confirmation of what it did,
true regardless of whether *our* process survived long enough to record its own attempt.
It only works for this if the correlating id (`reservation_id`, or an explicit payment
attempt id) is handed to the provider **at the start of the `charge()` call**, not
recovered from our own row afterward — otherwise a crash before that row exists leaves
the eventual webhook with nothing to correlate back to. Worth designing into BOOK-55
from the start, not bolted on after.

## 7. Compensating a *successful* charge, not just a declined one

**Found by a fair challenge: "how is this a saga if there's no compensation for a
successful charge?"** §6 fixed one gap (the payment record surviving a later crash) but
left the guest actually charged with nothing to show for it, if `confirm_reservation()`
then failed. Unlike §6's remaining gap, this one doesn't need a real provider to close
— we already have everything required, locally: a durable `Payment(SUCCEEDED)` row to
act on. BOOK-54's own description was updated to cover this explicitly, once it became
clear neither it nor BOOK-76 (guest-voluntary, policy-based refunds — a different
trigger entirely) actually tracked it.

**Chosen:** `saga.py`'s `react_to_payment_outcome()` wraps `confirm_reservation()` in
`try/except Exception` — broad on purpose, and not the usual anti-pattern, because:

1. What `confirm_reservation()` can realistically raise here (`ReservationNotFoundError`,
   `IllegalTransitionError`) is already near-impossible — the reservation was just
   fetched and checked `PENDING` moments earlier, in the same transaction. Anything that
   actually fires is either that freak race or a genuine infra failure — and both get
   the identical response.
2. The exception is **re-raised** after compensating, not swallowed — the real failure
   still surfaces as a `500`, still hits logs/monitoring. Broad-catch-and-hide is the
   anti-pattern; broad-catch-cleanup-then-rethrow isn't hiding anything.
3. Re-raising also avoids inventing a third HTTP response shape ("succeeded but we
   fixed it for you") for what should be a rare, exceptional path.

**`_refund_and_release()`:** rolls back the failed confirm attempt (discarding only its
own uncommitted changes — the payment from `create_payment_and_react`'s earlier,
already-committed transaction is untouched), issues a `Payment(kind=REFUND)` via
`provider.py`'s new `refund()`, releases the hold via `cancel_reservation_system()`, and
commits. The refund is itself another external, irreversible call — it inherits §6's
exact accepted gap symmetrically (a crash between `refund()` returning and *that*
commit), not a new problem to solve twice.

**Two real bugs found only by verifying this live, not caught by unit tests
(`scripts/verify_saga_commit_split.py`, extended rather than a new script — same
crash-simulation harness):**

- **Accessing `reservation.id` (or any attribute) immediately after
  `uow.session.rollback()` crashes with `MissingGreenlet`.** `rollback()`
  unconditionally expires every attribute on every persistent object in the session —
  not just ones touched in the failed transaction, `payment` included, even though it
  was already committed earlier by `create_payment_and_react`. The expired attribute's
  next read triggers a lazy reload, which async SQLAlchemy can't perform outside an
  awaited call — plain attribute access can't provide that. Fixed by capturing
  `reservation.id` and `payment.amount_cents` into plain variables *before* calling
  `rollback()`.
- **The verification script's own cleanup needed two more tables than expected** —
  `outbox_events` (the `BookingCancelled` event this path emits) and even
  `processed_notification_events` (BOOK-51's dedup marker), since the live
  `notification-consumer` actually picked up and processed the event for real during
  the test run. A reminder that "verify it live" surfaces integration details no fake
  session would.

## 8. BOOK-55: the webhook, and closing the gap it was built to close

The whole tree — every branch `react_to_payment_outcome()` /
`resolve_payment_and_react()` / `_refund_and_release()` can take, resolved either
synchronously or later by the webhook:

```
                    create_payment_and_react()
                              │
                    charge() called
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                      │
    SUCCESS               FAILURE            TIMEOUT / crash
        │                     │                      │
        │                     │              Payment = PENDING
        │                     │              (stuck here until a
        │                     │               webhook resolves it)
        │                     │                      │
        │                     │         resolve_payment_and_react()
        │                     │        (webhook says SUCCESS/FAILURE)
        │                     │                      │
        └──────────┬──────────┴──────────────────────┘
                   │
        react_to_payment_outcome()
                   │
      ┌────────────┴────────────┐
      │                          │
  SUCCEEDED                   FAILED
      │                          │
confirm_reservation()   cancel_reservation_system()
      │                          │
 ┌────┴─────┐              Reservation
 │          │              → CANCELLED
 ok      raises
 │          │
Reservation _refund_and_release()
→CONFIRMED       │
            create refund_payment (PENDING) → COMMIT
                  │
         provider_refund() called
                  │
        ┌─────────┴──────────┐
        │                     │
   resolves now          crashes → PENDING
        │                     │
 refund.status = outcome  (stuck here until a
        │                webhook resolves it)
        │                     │
        │          resolve_payment_and_react()
        │             (webhook, kind=REFUND)
        │                     │
        └──────────┬──────────┘
                   │
     refund.status set — nothing else happens
     (reservation is already CANCELLED, from
      the branch above, regardless of this)
```

Two rules apply everywhere on this tree, not just at one spot: any already-resolved
payment hit by `resolve_payment_and_react()` again is a pure no-op (a redelivered
webhook never redoes anything, anywhere in this tree), and only the `CHARGE` branch
(top half) ever touches the reservation — the `REFUND` branch (bottom half, under a
failed confirm) only ever updates its own row, since the reservation was already
decided one level up.

The charge path's two commit boundaries, zoomed in:

```
   BookIt / our DB                      Payment Provider

  Create Payment
  status=PENDING
        │
     COMMIT #1
        │
        ├──────────────── charge() ─────────────>│
        │                                         │
        │                                    (SUCCESS)
        │<────────────────────────────────────────┤
        │
  payment.status = SUCCEEDED
        │
     COMMIT #2
        │
        ▼
  Saga reaction (react_to_payment_outcome)
        │
        ▼
  Reservation → CONFIRMED
```

A crash anywhere before COMMIT #1 loses nothing real (no charge was ever attempted). A
crash anywhere *after* COMMIT #1 — including mid-`charge()`, including right after
COMMIT #2 but before the reaction finishes — always leaves a row on our side to recover
from: `PENDING` if it's before COMMIT #2, a resolved-but-unreacted `Payment` if it's
after. The webhook (via `resolve_payment_and_react()`) is what recovers the first case;
BOOK-54's existing split already recovers the second.

**Correlation id is `Payment.id` — no new id needed.** `service.create_payment()` already
calls `uow.session.flush()` before calling `charge()`, and SQLAlchemy applies `Payment.id`'s
Python-side default (`default=uuid.uuid4`) at flush time — so the id exists before the
provider is ever called. `charge()`/`refund()` now take `correlation_id` as a required
keyword-only argument even though `provider.py`'s mock never uses it for anything: the
point is the *shape*, not the mock's own behavior. The day a real provider replaces it,
every caller already hands over an id up front, exactly as `webhook-basics.md` §2 requires.

**Kept deliberately even though it's inert — worth being honest about the trade.**
`correlation_id` has zero behavior behind it today: our mock is a single Python function
returning a value in-process, not a separate system with its own database to store the
id in and echo it back from later. A real provider stores it and hands it back in its
own webhook call; ours doesn't need to, because the manual trigger script already knows
the `payment_id` directly. That makes this parameter speculative generality for a
provider that doesn't exist yet — the exact smell normally worth cutting. Kept anyway,
on the reasoning that it's cheap, already threaded through and tested, and removing it
now just means re-adding it (and re-touching every call site again) the day it's
actually needed.

**The HMAC signature is not the same kind of thing — it's live today, not shaped for
later.** Unlike `correlation_id`, both sides of it actually run right now:
`scripts/simulate_provider_webhook.py` computes a real signature and sends it;
`payment_webhook()` (`router.py`) verifies it and returns `401` on a mismatch. A request
to that endpoint without a valid signature is rejected today, mock provider or not —
it's the one thing standing between "anyone who finds the URL" and marking an unpaid
reservation as paid. Earns its place regardless of whether the provider ever becomes
real.

**Closing "crash before the first commit" now, not deferring it — the answer to the open
question §6/§7 both left for this ticket.** Both `service.create_payment()` and
`saga._refund_and_release()` used to flush the `Payment`/`refund_payment` row and only
commit it *after* calling the provider. A crash in that window rolled the row back with
it — nothing left for a webhook to resolve against, and no correlation id worth having.
Both now commit the row, PENDING, immediately after flush and *before* calling
`charge()`/`refund()`. This was originally going to stay an accepted gap until a real
provider existed (§6's framing), but there's no real reason to wait: the fix is a plain
reordering of two already-present lines, costs nothing, and converges "genuinely timed
out" and "crashed before recording the outcome" into the exact same recoverable state —
`PENDING`, an id, and a webhook that resolves either one identically.

**The refund path had the identical bug, not previously caught.** `_refund_and_release()`
called `provider_refund(amount_cents)` *before* `refund_payment` existed at all — so even
after closing the charge-side gap, a crash during the compensating refund would still
leave nothing to correlate a webhook against. Fixed the same way: mint `refund_payment`
PENDING, flush, commit, *then* call `provider_refund(amount_cents, correlation_id=refund_payment.id)`.

**`resolve_payment_and_react()` (`saga.py`) is the webhook's only entrypoint into the
saga — not a second copy of BOOK-54's logic.** Given `payment_id` + a resolved outcome:
no-op if the payment isn't `PENDING` (redelivery-safe, same discipline
`react_to_payment_outcome` already has); otherwise resolve the status, commit that on its
own (resolving what happened must survive even if reacting to it then fails — the same
split-commit reasoning as `create_payment_and_react`), then react **only if `kind ==
CHARGE`**. A `REFUND`'s webhook just records whether the money actually moved — nothing
downstream depends on it, since `_refund_and_release` already released the reservation's
hold unconditionally, regardless of how the refund itself resolves (unchanged from
BOOK-54).

**Confirmed live, not just reasoned about:** `scripts/verify_webhook_resolution.py`
proves three things against real Postgres, not fakes — (1) the early-commit fix means a
`Payment`/refund row survives a crash instead of vanishing, (2) a row left `PENDING` can
actually be resolved afterward by the webhook, (3) a repeat webhook delivery for the
same id is a no-op, not a second reaction. Same crash-simulation approach as
`verify_saga_commit_split.py` — patch `charge()`/`provider_refund()` to raise the
instant they're called, after the row is already committed.

**The webhook payload never accepts `TIMEOUT`.** `PaymentWebhookPayload.outcome` is typed
`Literal[PaymentOutcome.SUCCESS, PaymentOutcome.FAILURE]` — a webhook is definitionally
the provider's own resolution of what happened, never itself another "unknown"
(`webhook-basics.md` §1).

**Auth: HMAC-SHA256 over the raw body, not a shared-secret header.** `webhook_auth.py`
signs/verifies the exact bytes received, `hmac.compare_digest` for constant-time
comparison. Matches how real providers (Stripe et al.) actually authenticate their own
callbacks — chosen over a simpler shared-secret header specifically so this mock's
contract doesn't need to change when a real provider replaces it.

**Trigger: a manual script now (`scripts/simulate_provider_webhook.py`), deliberately
shaped for a later automatic one.** Nothing in this project schedules a delayed call to
the webhook automatically yet — a person (or a test) runs the script, which signs and
posts a payload. `send_webhook()` is factored out from the script's CLI specifically so
a future automatic trigger (e.g. a Celery task scheduled right after a `TIMEOUT`) can
import and call the same function instead of re-deriving the signing logic.

**Test coverage:** `tests/payment/test_webhook_auth.py` (signature round-trip, tampered
body, wrong signature) and `tests/payment/test_resolve_payment.py` (unknown payment id,
already-resolved no-op, `CHARGE` reacts, `REFUND` doesn't — `react_to_payment_outcome`
mocked out rather than re-testing BOOK-54's own internals). `test_provider.py` updated
for the new `correlation_id` parameter. Plus `scripts/verify_webhook_resolution.py`
(§ above) for what fakes can't cover. All passing.

**Revisit when:** a real provider replaces the mock — `correlation_id` already threads
through, but provider-side idempotency keys and reconciliation (§6's still-open items)
remain genuinely deferred until there's a real external system to protect against. Also
revisit the manual trigger once an automatic one is actually built — `send_webhook()` is
the seam that move should happen through.

## 9. BOOK-76: cancellation policies driving refund amounts

Every case `cancel_reservation_and_refund()` can hit, cancel to refund:

```
              Guest calls POST /reservations/{id}/cancel
                              │
              reservation_service.cancel_reservation()
              (ownership check + transition to CANCELLED --
               unchanged from BOOK-38)
                              │
              Was there a SUCCEEDED charge for this reservation?
                              │
                ┌─────────────┴─────────────┐
                │                           │
               NO                         YES
                │                           │
      refund_amount_cents = 0    Property.cancellation_policy
      (never charged --                     │
       e.g. still PENDING)      calculate_refund_cents(policy, check_in,
                                             now, charge.amount_cents)
                                             │
                          ┌──────────────────┼──────────────────┐
                          │                  │                  │
                      FLEXIBLE           MODERATE            STRICT
                          │                  │                  │
                    >=24h: 100%        >=5d: 100%          >=7d: 50%
                    <24h:  0%          >=24h: 50%          <7d:  0%
                                       <24h:  0%
                          │                  │                  │
                          └──────────────────┼──────────────────┘
                                             │
                          create_refund(uow, reservation, refund_amount_cents)
                          (mint Payment(REFUND, PENDING) -> COMMIT -> provider.refund())
                          -- runs even when the amount is 0
                                             │
                          ┌──────────────────┴──────────────────┐
                          │                                     │
                   resolves now                          crashes / times out
                   (SUCCEEDED/FAILED)                     -> stays PENDING
                          │                                     │
                          │                     resolve_payment_and_react()
                          │                     (BOOK-55's webhook, later)
                          └──────────────────┬──────────────────┘
                                             │
                          refund.status set -- nothing else reacts
                          (reservation already CANCELLED, from the top)
                                             │
                                             ▼
                          Response: ReservationCancelRead
                          { ...reservation fields, refund_amount_cents }
```

**This is not itself a saga, despite reusing a saga's machinery — worth being precise
about.** `saga-basics.md` §3's own test: a step needs saga treatment only if (1) something
downstream branches on its outcome, and (2) a successful outcome leaves a real effect
worth undoing if something later fails. `create_refund()`'s outcome here fails both —
the reservation is already `CANCELLED` before the refund is even attempted, so nothing
branches on whether it later resolves `SUCCEEDED`, `FAILED`, or sits `PENDING`; there's
no further compensating step if it fails. By §3's own definition this is a **terminal
action**, the same category that doc puts "most notifications" in — not a decision
point. What's actually reused from BOOK-54/55 isn't the saga *pattern*
(forward-step-plus-compensation) — it's the generic **external-call durability
machinery** underneath it: commit before calling out, a correlation id, and a webhook
that can resolve a stuck `PENDING` row later. That machinery is useful for any external
call, saga or not; `create_refund()` just happens to be shared by one of each.

**Webhook is not the same thing as saga — worth spelling out concretely, since this
flow makes the two easy to conflate.** A webhook is a mechanism for finding out a
result *later* from the provider; a saga is a pattern of orchestration + branching +
compensation once a result is known. This flow has the first without the second:

```
The cancel flow itself, no branching on the refund at all:

  User cancels
       │
  Reservation -> CANCELLED                (unconditional, happens first)
       │
  Was there a successful charge?
       │
      yes
       │
  calculate refund, then process it        (never: SUCCESS -> do X, FAILURE -> do Y)
```

`create_refund()`'s own internals can still hit the exact same async gap `charge()` can:

```
  Payment(REFUND) = PENDING
       │
     COMMIT
       │
  provider.refund()
       │
  TIMEOUT / process crash  -->  Payment stays PENDING
       │
  POST /payments/webhook  -->  resolve_payment_and_react()  (same endpoint, doesn't
       │                        care whether this row is a CHARGE or a REFUND)
       ▼
  Payment resolved
```

The two flows only diverge in what happens *after* that same webhook resolves something:

```
  CHARGE resolves SUCCESS            REFUND resolves (either outcome)
       │                                    │
  react_to_payment_outcome()          Payment.status updated
       │                                    │
  Reservation -> CONFIRMED            (nothing else -- reservation was
  (a real decision, made because           already CANCELLED before this
   the outcome was now known --            refund was even attempted)
   this is the saga)
```

So: a cancellation's refund uses the exact same webhook and durability infrastructure a
charge does, but the cancellation flow itself is not a saga. Same webhook, different
question — "did something happen yet" (webhook) is not "what do we do now that it has"
(saga).

**`cancellation_policy` lives on `Property`, not globally.** Matches the ticket's own
acceptance criteria, and how real platforms actually work — each host picks their own
policy for their own listing, not one platform-wide setting. Defaults to `MODERATE`
(`property/models.py`) so existing rows and hosts who don't pick one get a real middle
ground, not the most guest- or host-favorable option by default.

**The refund calculation is a pure function, no DB access at all.**
`reservation/cancellation_policy.py`'s `calculate_refund_cents()` takes a policy, a
check-in date, "now," and the charged amount — nothing else. Deliberately kept this way
so every boundary (the 18 tests in `test_cancellation_policy.py`) is checkable without a
database, a session, or any async machinery.

**`check_in` has no time-of-day — treated as midnight UTC of that date.** `Reservation.check_in`
is a `Date`, not a `DateTime`; the cutoffs ("24h before check-in") need an actual instant
to measure against. A deliberate simplification, not an attempt to model a real check-in
time (which varies per listing in reality) — documented in the module's own docstring so
it's not mistaken for an oversight later.

**Refund processing reuses BOOK-54/55's machinery, not a second implementation --
and lives in `payment/service.py`, not `saga.py`, once that became clear.**
`_refund_and_release()`'s "mint a REFUND payment, commit it PENDING, call the provider,
resolve its status" logic was extracted into `service.create_refund()` — the refund-side
mirror of `service.create_payment()`, the charge-side equivalent already there. Both
`_refund_and_release` (BOOK-54's system-triggered compensation, always the full amount)
and this ticket's guest-voluntary, policy-based partial refund call the identical
function. Putting it in `service.py` rather than `saga.py` matters for the same reason
the next paragraph does: `create_refund()` doesn't decide anything or branch on an
outcome — it's a mechanical "make this refund happen and record it" primitive, exactly
like `create_payment()` is for charges, not a saga concern. A refund created here that
times out is resolved later by exactly the same webhook (`resolve_payment_and_react`)
BOOK-55 already built — no new async-resolution path needed for refunds specifically.

**A zero-amount refund still creates a `Payment(REFUND)` row.** Considered skipping the
provider call entirely when the calculated amount is 0 (refunding nothing to a real
provider is a slightly odd call to make) — rejected in favor of one uniform code path:
every cancellation of a paid reservation produces a real, queryable record that the
policy was evaluated, rather than silence that later looks indistinguishable from a bug
("did we forget to check the policy?" vs. "we checked, and it was zero").

**Where the orchestration lives: a new `payment/cancellation.py`, not `reservation/service.py`
and, after the terminal-action distinction above became explicit, not `saga.py` either.**
`cancel_reservation_and_refund()` needs a `Payment` lookup and a `Property` lookup —
`reservation/service.py` deliberately stays payment-agnostic (the same import-direction
reasoning `saga.py`'s own module docstring gives for `react_to_payment_outcome`: payment
already depends on reservation, not the reverse, so adding the refund logic here avoids a
circular import rather than fighting one). It started out living in `saga.py` for
exactly that reason — but a file named `saga.py` implying something is a saga step when
it structurally isn't (no branching, no compensation) was its own kind of wrong, so it
moved to its own module once that became explicit, keeping "is this a saga decision"
answerable from file layout alone, not just a comment. `reservation_service.cancel_reservation()`
itself is completely unchanged — the guest ownership check and the actual `CANCELLED`
transition are still exactly BOOK-38's code, just called from one level up now.

**Refund amount returned in the same cancel response, not a separate preview
endpoint.** `ReservationCancelRead` adds one field (`refund_amount_cents`) to the normal
reservation response. A dedicated preview-then-confirm flow was considered and rejected:
the calculation is a pure function of policy + "how far ahead of check-in is *right
now*," so a preview call would still need to run again at actual-cancel time anyway (time
moves between the two calls, especially near a boundary) — a preview doesn't save any
work, it only adds a second place that has to agree with the first. The property's
policy terms are meant to be visible to the guest before booking (BOOK-99), not
discovered for the first time at the moment of cancelling.

**Real bug found and fixed while wiring this, not part of the original plan:**
`property/service.py`'s `create_property()` did `Property(host_id=host_id,
**data.model_dump())` — since the new `cancellation_policy` field is optional on
`PropertyCreate`, an omitted value dumps as `None` and gets passed to the model
constructor explicitly, overriding its own `MODERATE` default with an explicit `NULL` and
violating the column's `NOT NULL` constraint on insert. Fixed with `exclude_unset=True`,
matching what `update_property()` already did one function below it for the identical
reason. Confirmed live: created a property with no policy specified, verified it landed
as `MODERATE`, not a crash.

**Revisit when:** BOOK-99 (showing the policy in the UI) actually needs the exact
plain-language copy per tier — this doc's cutoffs are the source of truth for that copy,
not the other way around.

## 10. Two small, related fixes found by asking "why does paying feel instant?"

**Simulated provider latency.** `provider.py`'s mock resolved in zero time, which read
as fake rather than fast — a real card charge involves real network hops (card network,
issuing bank) that take actual time, even on the synchronous happy path. Added
`service._simulate_provider_latency()`: a random 0.4-1.2s `asyncio.sleep()` right before
calling `charge()`/`provider_refund()`, on both the charge and refund paths. Skipped
whenever `force=` is set, so `force=`-driven tests and verification scripts stay fast and
deterministic — the delay is for realism, not correctness, and has no business slowing
down anything that already knows its answer. This replaced an equivalent fake delay that
had been living in `BookingForm.tsx` (a `MIN_LOADING_MS` + `Promise.all` trick) — moved to
the backend so *any* client of this API gets realistic timing, not just this one button.

**`PendingChargeExistsError` — closing a retry gap `TIMEOUT` left open.** Once a charge
times out, the `Payment` stays `PENDING` but `react_to_payment_outcome()` no-ops on a
`PENDING` payment (by design — nothing to react to yet), which means the *reservation*
stays `PENDING`/payable too. Nothing was stopping a guest from clicking "Pay & confirm"
again in that state: `create_payment_idempotent()`'s dedup only fires on a *reused*
idempotency key, and the frontend mints a fresh one on every click, so a retry sails
straight past it into a second, fully independent charge attempt. Since `TIMEOUT`
specifically means "might have actually succeeded, we don't know," that second attempt
succeeding is a real double charge, with no record anywhere that a refund is owed.

**Chosen:** `create_payment()` now checks `uow.payments.get_pending_charge_by_reservation_id()`
right after the existing reservation-payable check, before minting a new `Payment` row.
A hit raises `PendingChargeExistsError` → `409` ("a previous payment attempt for this
reservation is still processing"). The repository method takes *any* matching row, not
`scalar_one_or_none()` — unlike the succeeded-charge lookup (backed by a real unique
index), nothing stops multiple `PENDING` rows from accumulating across retries, so this
can't assume at most one match.

**Why this was only fixable now, not a gap in the original design:** blocking "a second
attempt while an earlier one is unresolved" requires a durable record that an earlier
attempt *exists* — which is exactly what committing the row `PENDING` before calling the
provider (BOOK-55) already provides. The guard is new; the durability it depends on
isn't.

**Revisit when:** never for the mechanism — this is a permanent, always-needed guard, not
a stopgap. If BOOK-98's automatic `TIMEOUT` resolution ever gets built, this guard is what
makes the waiting period *safe*, not just visible.

## 11. BOOK-101: a reconciliation worker — the pull counterpart to BOOK-55's push

**The gap this closes:** the webhook (§8) is the only path back from `PENDING` today. If
it never arrives — lost delivery, provider outage, or (in dev) nobody runs
`scripts/simulate_provider_webhook.py` by hand — the `Payment` stays `PENDING` forever,
and BOOK-100's guard (§10) means the reservation is permanently stuck too. Real providers
say this explicitly: don't rely on webhooks alone. The standard answer is a reconciliation
job that periodically asks "what's the true status of X" for anything `PENDING` too long,
instead of only ever waiting to be told.

**The full flow, end to end:**
1. Celery Beat fires `app.worker.reconcile_stale_payments` every 60s (`celery_app.py`'s
   `beat_schedule`, same mechanism as BOOK-46's `expire_pending_reservations`).
2. The task opens a `UnitOfWork` and delegates to `saga.reconcile_stale_payments(uow,
   older_than)`, where `older_than` is now minus a 5-minute threshold.
3. `uow.payments.list_stale_pending(older_than)` finds every `Payment` still `PENDING`
   past that cutoff — both `CHARGE` and `REFUND`, either can get stuck this way.
4. For each one, `provider.check_status()` asks the mock "what really happened" — and
   `resolve_payment_and_react()`, the *same function the webhook itself calls*, resolves
   it and (for a `CHARGE`) confirms or cancels the reservation.

**Chosen: reuse `resolve_payment_and_react()`, don't duplicate it.** Reconciliation isn't
a second way to resolve a payment — it's a second way to *trigger* the one resolution
path that already exists, redelivery-safe guard included for free.

**Chosen: `check_status()` can never return `TIMEOUT`.** `charge()`/`refund()` can time
out because they're a fresh attempt that might not get an answer in time. A status
*check* is a lookup against the provider's own already-decided record — a real provider's
equivalent (e.g. "retrieve payment intent") always returns a definite state — so the mock
only rolls `SUCCESS`/`FAILURE` (90/10).

**Two knobs, not one:** the task *runs* every 60s, but only *acts* on payments already
past the 5-minute threshold. Frequent runs keep detection latency low once something is
actually stuck; the threshold keeps it from ever touching a payment still normally
in flight.

**Doesn't replace the manual webhook trigger.** §8's "Revisit when" row (the webhook
trigger row in the cheat sheet below) anticipated "an automatic trigger" for the webhook
itself — this isn't that. `scripts/simulate_provider_webhook.py` still simulates the
provider calling *us*; BOOK-101 is a wholly separate path for when that call never
comes, not a replacement for it.

**Testing:** `reconcile_stale_payments()`'s own logic (find stale rows, check each,
resolve each) is unit-tested — no-stale-payments, multi-payment resolution, and a stale
`REFUND` resolving without touching a reservation. Verified live against the real
database too: a payment backdated past the threshold resolved correctly (`SUCCEEDED` →
reservation `CONFIRMED`), while a freshly created `PENDING` payment was correctly left
untouched. That live run also surfaced an unrelated pre-existing debris row (a duplicate
`PENDING` charge from days earlier, predating BOOK-100's guard) — cleaned up as test
hygiene, not a bug in this ticket.

**Revisit when:** a real payment provider exists — `check_status()`'s mock weights and
the 5-minute threshold are both guesses standing in for a real provider's actual API and
actual latency characteristics.

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| BOOK-52 outcome selection | Weighted random (90/8/2), `force=` for determinism | Never for the mechanism |
| Simulated provider latency | Random 0.4-1.2s before calling charge()/refund(), skipped when `force=` is set | Never — realism for real use, zero cost to tests |
| Retry racing an unresolved `TIMEOUT` | `PendingChargeExistsError` (409) blocks a second charge attempt while one's still `PENDING` | Never — permanent guard, not a stopgap |
| BOOK-52 TIMEOUT vs FAILURE | Two distinct outcomes, not one — timeout means "unknown," not "confirmed failed" | Never — this distinction is load-bearing for BOOK-53's idempotency design |
| BOOK-53 `IdempotencyKeyStatus.COMPLETED` on timeout | Marked COMPLETED anyway — it tracks the request attempt, not the payment outcome | Never — a retry must not re-charge an unresolved payment regardless of this naming |
| `IdempotencyKey.expires_at` cleanup | Daily delete (`core/idempotency/cleanup.py`), not archive | A real need for auditing expired attempts shows up |
| Reused key, different request body | Explicit `400` (`...MismatchError`), both reservations and payments | Never — matches Stripe's documented behavior for this case |
| BOOK-54 saga trigger | Orchestration (one function, explicit sequencing) | A second real independent consumer of the payment outcome becomes real |
| BOOK-54 compensating action | New internal helper (shared with BOOK-46's expiry job), not `cancel_reservation()` | Never — that function's ownership check is specifically for the guest HTTP path |
| BOOK-54 saga idempotency | Check reservation status before acting; already-moved-on is a no-op | Never — required for BOOK-53's retries and BOOK-55's redeliveries alike |
| BOOK-54 commit split | Two local transactions (charge, then react), not one | Never — matches saga-basics.md §2's own shape |
| Compensating a successful charge | Refund + release, `except Exception` then re-raise | Never — response is identical regardless of cause, and re-raising means nothing is hidden |
| `rollback()` and expired attributes | Capture ids/amounts into plain variables before rolling back | Never — `rollback()` expires every persistent object's attributes, not just the failed transaction's |
| BOOK-55 correlation id | `Payment.id`, already available post-flush — no new id needed | `charge()`/`refund()` require it as a param regardless, so the shape is right when a real provider arrives |
| BOOK-55 crash-before-commit gap | Closed now, not deferred — commit the row PENDING before calling the provider (charge *and* refund paths) | Never — the fix was a free reordering, no reason to wait for a real provider |
| BOOK-55 webhook entrypoint | One function, `resolve_payment_and_react()` — no second copy of BOOK-54's logic | Never — CHARGE reacts via the existing saga, REFUND just resolves its own status |
| BOOK-55 webhook payload | `outcome` excludes `TIMEOUT` by type | Never — a webhook is always a final answer, never another "unknown" |
| BOOK-55 webhook auth | HMAC-SHA256 over the raw body, not a shared-secret header | Never — matches real providers' own convention, no contract change needed later |
| BOOK-55 webhook trigger | Manual script (`scripts/simulate_provider_webhook.py`), `send_webhook()` factored out | Once an automatic trigger (e.g. Celery-scheduled) is built — swap the caller, not the signing logic |
| BOOK-76 policy ownership | Per-`Property`, defaults `MODERATE` | Never — matches real platforms, each host sets their own listing's terms |
| BOOK-76 refund calculation | Pure function (`cancellation_policy.py`), no DB access | Never — keeps every boundary unit-testable without a database |
| BOOK-76 `check_in` as an instant | Treated as midnight UTC of that date | If a real per-listing check-in *time* ever gets modeled |
| BOOK-76 refund processing | Reuses BOOK-54/55's `create_refund()`/webhook, not a second implementation | Never — a timed-out policy refund resolves via the exact same webhook |
| BOOK-76 zero-amount refund | Still creates a `Payment(REFUND)` row | Never — a real record beats silence that looks like a missed check |
| BOOK-76 orchestration location | New `payment/cancellation.py` — not `reservation/service.py` (import direction), not `saga.py` (not a saga step) | Never — file layout should show "is this a saga decision" without needing a comment |
| BOOK-76 `create_refund()` location | `payment/service.py`, mirroring `create_payment()` | Never — it's a mechanical primitive, not a saga concern |
| BOOK-76 refund preview | Same response as the cancel action, no separate preview call | Never — a preview would still have to recompute at actual-cancel time anyway |
| BOOK-101 reconciliation trigger | Celery Beat, 60s, same mechanism as BOOK-46's expiry job | Never — matches the existing periodic-task pattern |
| BOOK-101 resolution logic | Reuses `resolve_payment_and_react()` — no second copy of BOOK-55's logic | Never — one resolution path regardless of push (webhook) or pull (this worker) |
| BOOK-101 `check_status()` outcomes | `SUCCESS`/`FAILURE` only, never `TIMEOUT` | Never — a status check answers about an already-decided outcome, unlike a fresh attempt |
| BOOK-101 staleness threshold | 5 minutes, checked every 60s | A real provider exists with known latency characteristics |
