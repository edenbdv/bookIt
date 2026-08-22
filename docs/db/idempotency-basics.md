# Idempotency basics (BOOK-36)

Written as theory before touching anything, same approach as `exclude-constraint-basics.md` — updated in place once the actual BOOK-36 work happens.

## 1. The problem: a client can't always tell if its request succeeded

A guest clicks "Book now." The request reaches the server, the reservation is created — but the response is slow, or the connection drops, before it gets back to the client. From the client's point of view, this is indistinguishable from the request never having reached the server at all. So what does it do? Retry — which is the only reasonable thing a client *can* do, since it genuinely doesn't know which happened. Without anything else in place, that retry creates a **second, real reservation**, for something the guest only meant to do once.

This is a different problem from BOOK-35's: BOOK-35 is about two *different* guests racing for the *same* dates. This is about the *same* guest's *own* request being retried — deliberately or automatically — and the server needing to recognize "I've already done this."

## 2. Why disabling the submit button isn't enough

It's tempting to think the frontend already solves this — disable the button after one click, done. That helps, but only as a UX nicety, not a guarantee, because several real situations bypass it entirely:

- The network is slow: the user sees nothing happen for several seconds, assumes it failed, and clicks again (or refreshes the page) before the first response — and often before the disabling JS — ever lands
- The browser tab crashes or reloads mid-request; the user retries with no idea whether the first attempt landed
- Many HTTP client libraries (especially mobile networking stacks) **automatically retry a timed-out request**, transparently, with no user interaction and no button involved at all
- A request can be fired directly (curl, a buggy client, a malicious one) that never goes through the button in the first place

Same shape as the reasoning in `exclude-constraint-basics.md` §5 and `concurrency-basics.md`: an app-level (here, frontend-level) "try to stop it before it happens" measure can't provide a real guarantee, because it depends on every calling path behaving correctly. Only something the *server* enforces, independent of client behavior, actually closes the gap. Disabling the button reduces how often a duplicate request even reaches the server (genuinely useful — less load, less redundant work) — it just isn't the correctness mechanism.

## 3. What an idempotency key actually does

The client generates a random key (e.g. a UUID) once, before sending the request, and attaches it (conventionally an `Idempotency-Key` header). The server's rule: **the same key always produces the same result, no matter how many times it's submitted.**

- First request with a given key: processed normally — the reservation is created, and the key is recorded alongside the resulting `reservation_id`
- Any later request with the **same** key: the server recognizes it's already been handled, and returns the *original* reservation (same response, same status code) — the booking logic itself never runs a second time, so there's no second row, no second exclusion-constraint check, nothing

Critically, this works regardless of *why* the request was repeated — button not disabled, network retry, client bug, deliberate resubmission. The key doesn't care about intent; it only cares "have I seen this exact key before."

**When does the client generate a new key vs. reuse one?** This is entirely the client's responsibility — the server never generates or assigns keys, it only remembers whatever it's given. The trigger is: **does the client already know how the previous attempt ended?**

- **Reuse the same key** while the outcome is still *unknown* — the request is in flight, timed out, the connection blipped, or the user impatiently clicked "Book now" again before the first click's response arrived. As far as the client can tell, it might be re-asking about the exact same attempt, so it must use the exact same key.
- **Generate a new key** once the outcome *is* known, either way:
  - **Success** (201 back) — that attempt is done. Booking something else next (different dates, different property, or even a separate booking of the same property) is a new click → new key.
  - **Real failure** (400/403/404/409 — a definitive "no" actually reached from the server, e.g. dates unavailable) — that attempt is also done, just unsuccessfully. If the user adjusts something and clicks "Book now" again, that's a new attempt → new key.

One-sentence version: **a key's lifetime is exactly one click, from firing the request until a real response comes back.** Anything that resends before that point reuses the key; anything after it is a new click and needs a new key. Reusing an old key for what should be a new booking would make the server treat it as the same request and hand back the old result instead of creating anything new.

## 4. Where the key gets stored — the open question for BOOK-36

Recording "this key already produced this reservation" needs somewhere durable to live, checked on every incoming request before the booking logic runs at all.

**Industry standard: a dedicated idempotency-keys table, not a column on `Reservation`.** This is the pattern Stripe's widely-copied `Idempotency-Key` design uses, and most payment/booking APIs that follow it do the same. Rather than bolting an `idempotency_key` column onto one specific resource, a separate table generalizes across *any* endpoint that might need this later (bookings, payments, whatever comes next):

```
IdempotencyKey
  key            (the client-supplied key, unique)
  status         ("processing" | "completed" -- see the race condition below)
  response_status
  response_body
  created_at
  expires_at
```

Storing the **actual response** (not just a reference to the created row) matters: a retry then gets back byte-for-byte what the original call returned, even if the resource's serialization logic changes later.

**Durable, but with a real expiration window — not a short cache TTL, not forever.** Stripe keeps idempotency keys for roughly 24 hours: long enough that a client retrying hours later, after a network partition, still works; short enough that the table doesn't grow forever. That rules out defaulting to Redis's usual short TTLs just because Redis exists elsewhere in this stack (Epic 5) — but it's not "keep every key permanently" either.

**The concurrent-duplicate-key race.** What if the *same* key arrives twice at nearly the same instant — a genuine retry firing before the first response even comes back? An app-level "have I seen this key before?" check has the identical race window already covered in `exclude-constraint-basics.md` §5 and `concurrency-basics.md`: two concurrent requests can both see "not seen yet" before either writes. The fix is the same shape as BOOK-35's `EXCLUDE` constraint: a `UNIQUE` constraint on the `key` column, so the second insert attempt fails immediately at the DB level (or that request waits/polls for the first to finish) — the booking logic itself never actually runs twice concurrently for the same key.

This is a real design decision to make explicitly in BOOK-36, not default into.

**Scoping the key to its owner.** The `key` column alone is the primary key — nothing ties it to *which guest* created it. Without an extra check, a different guest who reuses or guesses someone else's key would get back that other guest's reservation as the result of their own request. BOOK-36's implementation guards against this: before returning a `completed` key's reservation, it checks the reservation's `guest_id` matches the requester. A mismatch is treated identically to "this key is still processing" (same 409, same message) rather than a distinct error — so the response never reveals whether the key exists at all, only that *this* guest can't use it.

## 5. A failed attempt doesn't permanently burn the key

BOOK-36's implementation inserts the `IdempotencyKey` row *before* running the booking logic (status `processing`), then flips it to `completed` only after the booking actually succeeds. What happens if the booking fails in between — bad property, self-booking, a genuine date conflict?

The key point: **`flush()` is not `commit()`.** Flushing sends the `processing` row to Postgres so it can be checked (e.g. so a concurrent duplicate key's `INSERT` actually collides with it), but it isn't durable yet. This project uses one `Session`/one transaction per request (`UnitOfWork`, `docs/...`); if any exception escapes the route handler, `UnitOfWork.__aexit__` calls `rollback()`, which undoes *everything* done in that transaction since the last commit — not just the statement that failed. That includes the `processing` row from the same request.

So after a failed attempt, no row for that key exists in the table at all — as if the request never happened. A retry with the exact same key starts completely fresh: the insert succeeds cleanly (nothing to collide with), and the booking logic runs from scratch. The key only becomes permanently "used" once the request as a whole succeeds and commits.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Idempotent request | Submitting it once or many times with the same key produces the same result — no duplicate side effect |
| `Idempotency-Key` | A client-generated key (e.g. UUID) sent with the request, letting the server recognize a retry of the *same* logical request |
| BOOK-35 vs. BOOK-36 | BOOK-35: two *different* guests racing for the same dates. BOOK-36: the *same* guest's request retried — different problem, different fix |
| Button-disabling | A genuinely useful UX reduction in duplicate requests reaching the server — not a correctness guarantee, since it depends on every client path behaving correctly |
| Dedicated idempotency-keys table | Industry-standard shape (Stripe et al.) — generalizes across endpoints and stores the actual response, not just a reference to the created row |
| Expiration window | Durable but not permanent — Stripe's is ~24h; long enough for a delayed retry, short enough not to grow forever |
| Concurrent-key race | Same key arriving twice at once — fixed with a `UNIQUE` constraint on the key column, same DB-level-not-app-level reasoning as BOOK-35 |
| `flush()` vs `commit()` | Flush sends SQL to Postgres (so constraints can fire) but isn't durable; a later rollback in the same transaction erases it too — this is why a failed booking attempt doesn't permanently burn its idempotency key |
