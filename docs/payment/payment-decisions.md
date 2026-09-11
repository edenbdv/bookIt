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

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| BOOK-52 outcome selection | Weighted random (90/8/2), `force=` for determinism | Never for the mechanism; add real latency if a retry/backoff ticket needs to exercise actual timing |
| BOOK-52 TIMEOUT vs FAILURE | Two distinct outcomes, not one — timeout means "unknown," not "confirmed failed" | Never — this distinction is load-bearing for BOOK-53's idempotency design |
