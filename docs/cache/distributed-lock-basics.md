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

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `SET NX PX` | Set-if-absent with an expiry — the distributed-lock primitive. Release via Lua compare-and-delete, never bare `DEL` |
| Redlock critique | A Redis lock can be lost to TTL expiry or failover without the holder noticing → optimization, never a correctness boundary |
| The general rule | Use a distributed lock to avoid unnecessary work, never to guarantee correctness — if correctness depends on it, the design is wrong |
