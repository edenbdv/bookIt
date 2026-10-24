# Redis as a lock — and why it's the weakest of the three (BOOK-43)

Split out of `redis-basics.md` — this is the third and last of Redis's jobs in this project (cache: `cache-aside-basics.md`; counter: `rate-limiting-basics.md`; lock: here), and the one with the most important caveat.

## The mechanism

BOOK-43's role. The mechanism is `SET key <unique-token> NX PX <ttl>`:

- `NX` = only set if the key doesn't exist. Succeeds for exactly one caller; everyone else gets nil. That's the mutual exclusion.
- `PX <ttl>` = auto-expire, so a holder that crashes doesn't wedge the lock forever.
- The `<unique-token>` matters: release must be a Lua compare-and-delete ("delete only if the value is still *my* token"), never a bare `DEL`. Otherwise a slow holder whose TTL expired can delete a lock that a *different* holder now legitimately owns.

## And it still isn't a correctness guarantee

Two ways it silently fails:

1. Holder A's TTL expires while A is still working (a GC pause, a slow query). Redis hands the lock to B. Now A and B both believe they hold it.
2. A failover promotes a replica that hadn't yet received A's `SET NX`. B acquires the "same" lock from the new primary.

This is the Redlock debate (Kleppmann vs. Antirez) in two bullets. The takeaway: **a lock that can be lost without the holder noticing is a performance optimization, not a safety property.** Which is precisely why BOOK-43's description says booking correctness must keep resting on the `EXCLUDE` constraint (`app/reservation/models.py:50-56`) and the existing `pg_advisory_xact_lock` (`app/reservation/service.py:134-137`), with Redis as at most a third, advisory layer.

Worth internalizing as the general rule, since it's the one people get wrong: **use a distributed lock to avoid unnecessary work, never to guarantee correctness.** If correctness depends on the lock, the design is already wrong.

## Why booking creation uses a Postgres advisory lock, not a Redis one

The obvious follow-up question, given that `create_reservation` *does* take a lock (`app/reservation/service.py:134-137`) — why that one and not Redis? Four reasons, in increasing order of how load-bearing they are:

1. **No new dependency that can fail.** If Redis goes down, someone has to decide between blocking all bookings and failing open — which silently removes the protection. Postgres is already a hard dependency: if it's down there is no booking either way.
2. **No TTL to guess.** Every Redis lock needs one, and there is no right value. Too short and it expires while the holder is still working (failure mode 1 above). Too long and a crashed holder wedges everyone for its full duration. `pg_advisory_xact_lock` has no TTL at all — the *xact* in its name is the point: its lifetime is the transaction, released automatically on commit or rollback.
3. **It costs one line of SQL.** No client library, no per-holder token, no Lua release script.
4. **The deepest one: even a *perfect* Redis lock wouldn't help here.** What needs protecting is the window between the `INSERT` and the `COMMIT` of a Postgres transaction — that's the span in which two overlapping uncommitted GiST entries can deadlock against each other (`concurrency-basics.md` §4.1 has the full mechanism). Only Postgres knows when that window opens and closes. A lock living in another system cannot be aligned to a boundary it can't observe.

Generalized: **a Redis lock coordinates things that don't share a transactional store.** Two concurrent `create_reservation` calls share the same Postgres, so there is no reason to leave it.

## A lock is not how you get exactly-once

The other tempting use — "lock around the call to the payment provider so we don't charge twice" (Epic 8) — doesn't work either, and for a reason unrelated to the Redlock critique.

A lock solves exactly one problem: **two things happening at the same time.** Duplicate charges are not that problem:

```
1. call the provider: charge 500
2. the network times out
3. you have no idea whether the charge went through
4. you retry
5. → the customer is charged twice
```

Those two calls were *sequential*. The lock was acquired, released, and re-acquired — it was free at every instant and prevented nothing. And in the one case where the calls do overlap, it's because the provider is slow, which is precisely when the TTL is most likely to expire mid-call and let a second worker in.

**What actually works: idempotency at the receiver.** Every serious payment provider accepts an `Idempotency-Key`; you generate it once and send it with *every* retry, and the provider returns the original result instead of charging again. Same idea as the fencing tokens Kleppmann proposes — the guarantee has to live at the resource, not at the caller.

The principle, which is the real reason the lock fails: **you cannot get exactly-once by locking the sender. You get it by making the operation idempotent at the receiver.** A lock buys "at most one at a time", which is a strictly weaker and different thing.

Both halves of this already exist locally — the partial unique index at `services/payment/src/payment/models.py:32-38` (at most one succeeded charge per reservation) and `create_reservation_idempotent` (`app/reservation/service.py:159`). Epic 8's remaining piece is carrying that key outward to the provider. A lock never enters the picture.

## When a Redis lock *is* the right tool

The sections above are all "not here" — worth being equally concrete about where it genuinely belongs. Note what these have in common: losing the lock costs **wasted work, money, or load** — never corrupted data.

- **Cache stampede.** A hot key expires, thousands of requests miss simultaneously, and all of them start the same expensive recomputation — enough to take the database down, failing unrelated requests too. With a lock, one computes and the rest wait for the result. This is the case where a Redis lock is genuinely load-bearing for availability.
- **Scheduled jobs across N replicas.** Ten instances, one nightly job. Without coordination all ten run it — ten times the emails, ten times the API bill. There's no row in a database to hang a lock on, so Redis is the standard answer. If it double-runs once a year, you survive.
- **Rationing an expensive external resource.** "Only one worker may call this API at a time." Cost and rate limits, not correctness.
- **No shared transactional store.** Services on separate datastores with no common transaction to attach a lock to. Redis is the neutral ground.
- **Keeping work off the database entirely.** A connection from the pool is a scarce resource; a Redis key is cheap. Gating expensive work *before* it reaches Postgres is a legitimate reason not to use a Postgres lock for it.

## The one question that decides it

> What happens if the lock is silently lost?

- **"Corrupted data."** → Redis is the wrong tool, always. Use a database constraint, idempotency keys, or a consensus store (etcd/ZooKeeper).
- **"Duplicate work, wasted money, or load."** → Redis is exactly the right tool, and occasionally essential.

Applied to BOOK-43, the answer is "a double booking" — category one. Which is why the `EXCLUDE` constraint is what makes booking correct, and why that ticket is a technique exercise rather than a fix.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `SET NX PX` | Set-if-absent with an expiry — the distributed-lock primitive. Release via Lua compare-and-delete, never bare `DEL` |
| Redlock critique | A Redis lock can be lost to TTL expiry or failover without the holder noticing → optimization, never a correctness boundary |
| The general rule | Use a distributed lock to avoid unnecessary work, never to guarantee correctness — if correctness depends on it, the design is wrong |
| The deciding question | "What happens if the lock is silently lost?" Corrupted data → wrong tool. Wasted work/money/load → right tool |
| Why Postgres won here | A Redis lock coordinates things that *don't* share a transactional store. Two `create_reservation` calls share one Postgres — and the window needing protection (`INSERT`→`COMMIT`) is one only Postgres can observe |
| No TTL to guess | `pg_advisory_xact_lock`'s lifetime is the transaction itself. Every Redis lock needs a TTL, and there's no right value — too short expires mid-work, too long wedges everyone |
| Locks ≠ exactly-once | Duplicate charges come from *sequential* retries after an ambiguous timeout, not concurrency. Fix with idempotency at the receiver, not a lock on the sender |
| Where it's genuinely right | Cache stampede, cron across N replicas, rationing an expensive external API, services with no shared datastore |
