# Cache-aside: TTL, hit rate, and when it's worth it (BOOK-40)

Split out of `redis-basics.md` — read that first for what Redis *is* and the data types/commands (§1–3), and for the RAM-vs-work argument that decides whether caching a query is worth it at all. This file is specifically the cache-aside *pattern*: how to use a TTL, what "hit rate" means, and the two ways it can go wrong.

## 1. TTL is the single most important concept here

Every key can carry an expiry. When it passes, the key is gone — no job to run, no cleanup to write.

This is what makes Redis a *cache* rather than a second database, and it's doing more work than it appears to:

- **It bounds staleness.** A cached value is a copy of something that can change. A 60-second TTL is a promise that the copy is never more than 60 seconds behind — without writing a single line of invalidation logic. That's why BOOK-40 can ship usefully with no invalidation at all, and why `cache-invalidation-basics.md`'s honest baseline is "TTL only."
- **It bounds memory.** Keys clean themselves up. Nothing accumulates forever.
- **It's a safety net under invalidation.** However clever `cache-invalidation-basics.md`'s scheme is, some path will eventually be missed. The TTL is the backstop that makes a missed invalidation a temporary wrong answer instead of a permanent one.

Choosing a TTL is therefore a product decision, not a technical one: *how stale is this answer allowed to be?* A property listing that's 30 seconds out of date is fine. A payment status that's 30 seconds out of date is not.

Contrast with `IdempotencyKey.expires_at` (`app/reservation/models.py:84`): that's a ~24h window on a **Postgres** row, and `idempotency-basics.md` §4 explicitly warns against reaching for Redis's short TTLs there just because Redis is in the stack. Idempotency keys must survive a restart; cache entries needn't. Different requirement, different store.

## 2. Cache-aside, and the two failure modes it has

**Cache-aside** (also "lazy loading") is the pattern BOOK-40 names, and it's the one you want. In pseudocode, at the service layer:

```
value = redis.get(key)
if value is not None:          # hit
    return deserialize(value)
value = <the real query>       # miss
redis.set(key, serialize(value), ex=TTL)
return value
```

That's it. The application owns the logic; Redis knows nothing about Postgres. (The alternatives — read-through/write-through, where the cache itself fetches from the DB — need a caching layer that has a database connection. Not what's being built here.)

**Hit, miss, hit rate.** A **hit** is `GET` returning a value. A **miss** is it returning nil — either the key was never written, or its TTL passed and Redis dropped it. There's no third outcome; from the app's side "expired" and "never existed" are indistinguishable, which is exactly what makes TTL-based expiry so cheap to reason about.

The ratio of hits to total lookups is the **hit rate**, and it's the number that decides whether a cache is worth having at all:

- Cost of a **hit**: one Redis round trip (~0.5ms), and you skip the query.
- Cost of a **miss**: one Redis round trip, *plus* the real query, *plus* a second round trip to write the result back.

So a miss is strictly more expensive than not caching. At a 90% hit rate that's an obvious win. At 20% you've added latency, a moving part, and a staleness bug surface — to make things slower.

**This is the real argument to weigh in BOOK-40's open question.** Every dimension added to a cache key multiplies the key space: `city × guests × min_price × max_price × sort × limit × offset` is already large, and adding `check_in`/`check_out` multiplies it by every date range anyone might type. A key space much larger than your traffic means nearly every lookup is a miss on a key no one will ever request twice — a cache that only ever pays costs. Hit rate, not elegance, is what should decide that ticket — which is exactly what BOOK-88 (the measurement spike blocking BOOK-40) exists to pin down before any code gets written.

Two more properties worth naming:

- **Only requested data is ever cached.** Nothing is pre-warmed, so the cache stays proportional to real traffic.
- **A cold cache means every request is a miss** — plus the write. The first requests after a deploy or a Redis restart are *slower* than no cache at all. Fine at this scale; the reason big systems care is the thundering herd below.

**Caching "nothing" is a decision.** A filter combination matching zero properties is still an answer, and re-deriving it costs the same query as any other. But if a miss is represented as "no key present," you can't tell "not cached yet" apart from "cached, and the answer is an empty list" — so every request for that filter re-runs the query, forever. Storing an explicit empty value (**negative caching**) fixes it, and incidentally closes a cheap denial-of-service: hammering a nonsense filter otherwise bypasses the cache entirely by construction. Decide it deliberately in BOOK-40 rather than inheriting whichever behaviour falls out of `if value is not None`.

**Failure mode 1: the stale window.** Between a write to Postgres and the cache entry expiring or being invalidated, reads return the old answer. This is not a bug to eliminate — it's the trade being made. It just has to be a *known* size (§1 above) rather than an unbounded one.

**Failure mode 2: the thundering herd (a.k.a. cache stampede).** A popular key expires. Fifty concurrent requests all miss at the same instant, all run the same expensive query, and all write the same value back. The cache made the worst case *worse*, because the misses now cluster instead of being spread out. Mitigations are staggered TTLs (add jitter), or letting one request rebuild while others serve the stale value. Not required for BOOK-40 at this traffic level — worth recognizing by name, because the symptom (periodic DB load spikes at regular intervals) is otherwise baffling.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| TTL | Per-key expiry. Bounds staleness, bounds memory, and backstops any missed invalidation — the core of why a cache is safe |
| Cache-aside | Check cache → on miss, query DB → write result back with a TTL. App owns the logic; Redis knows nothing about Postgres |
| Cache hit / miss | `GET` returned a value / returned nil. "Expired" and "never existed" are indistinguishable from the app's side |
| Hit rate | Hits ÷ lookups. A miss costs *more* than no cache (round trip + query + write-back), so a low hit rate makes things slower |
| What a cache actually saves | Query *execution* (joins, aggregates, sorts) — not disk access. Postgres serves from RAM too — see `redis-basics.md` §1 |
| Negative caching | Storing an explicit empty result, so a zero-match filter doesn't re-query forever and can't be used to bypass the cache |
| Stale window | The gap between a DB write and the cached copy catching up — the trade being made, acceptable only when its size is known |
| Thundering herd | A hot key expires and N concurrent requests all rebuild it at once. Mitigated with TTL jitter or single-flight rebuild |
