# What BookIt chose for payments, and why

The payment-domain companion to `docs/jobs/worker-decisions.md` — same shape: what was
picked, what was rejected, and when to revisit. See `docs/domain-layer/reservation-lifecycle.md`
for how this epic plugs into the reservation state machine Epic 4 already built —
Epic 8 replaces *who calls* `confirm_reservation()`/`cancel_reservation()`, not the
functions themselves.

## 1. BOOK-52: the mock provider's outcome is weighted-random by default, force-able for callers that need determinism

**Chosen:** `app/payment/provider.py`'s `charge(amount_cents, *, force=None)` returns
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
(`app/payment/saga.py`) explicitly calls the charge step, then explicitly decides
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

**Chosen:** `app/payment/saga.py`'s `create_payment_and_react()` commits right after
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

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| BOOK-52 outcome selection | Weighted random (90/8/2), `force=` for determinism | Never for the mechanism; add real latency if a retry/backoff ticket needs to exercise actual timing |
| BOOK-52 TIMEOUT vs FAILURE | Two distinct outcomes, not one — timeout means "unknown," not "confirmed failed" | Never — this distinction is load-bearing for BOOK-53's idempotency design |
| BOOK-53 `IdempotencyKeyStatus.COMPLETED` on timeout | Marked COMPLETED anyway — it tracks the request attempt, not the payment outcome | Never — a retry must not re-charge an unresolved payment regardless of this naming |
| `IdempotencyKey.expires_at` cleanup | Daily delete (`core/idempotency/cleanup.py`), not archive | A real need for auditing expired attempts shows up |
| Reused key, different request body | Explicit `400` (`...MismatchError`), both reservations and payments | Never — matches Stripe's documented behavior for this case |
| BOOK-54 saga trigger | Orchestration (one function, explicit sequencing) | A second real independent consumer of the payment outcome becomes real |
| BOOK-54 compensating action | New internal helper (shared with BOOK-46's expiry job), not `cancel_reservation()` | Never — that function's ownership check is specifically for the guest HTTP path |
| BOOK-54 saga idempotency | Check reservation status before acting; already-moved-on is a no-op | Never — required for BOOK-53's retries and BOOK-55's redeliveries alike |
| BOOK-54 commit split | Two local transactions (charge, then react), not one | Never — matches saga-basics.md §2's own shape |
| Crash before the first commit | Accepted gap for now — mock provider, nothing to reconcile against | A real payment provider replaces the mock — build provider-side idempotency + reconciliation then |
| BOOK-55 scope | Not just TIMEOUT — the actual source of truth for "crashed before commit" too | Never — design the correlation id in from the start, not bolted on after |
