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

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| BOOK-52 outcome selection | Weighted random (90/8/2), `force=` for determinism | Never for the mechanism; add real latency if a retry/backoff ticket needs to exercise actual timing |
| BOOK-52 TIMEOUT vs FAILURE | Two distinct outcomes, not one — timeout means "unknown," not "confirmed failed" | Never — this distinction is load-bearing for BOOK-53's idempotency design |
| BOOK-53 `IdempotencyKeyStatus.COMPLETED` on timeout | Marked COMPLETED anyway — it tracks the request attempt, not the payment outcome | Never — a retry must not re-charge an unresolved payment regardless of this naming |
