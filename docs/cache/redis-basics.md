# Redis & caching basics (Epic 5 / BOOK-7)

Written as theory before touching anything, same approach as `exclude-constraint-basics.md` and `idempotency-basics.md` — updated in place once the actual BOOK-40/41/42/43 work happens.

Assumes the general idea of a cache is already familiar ("keep a copy of an expensive answer somewhere fast"). What follows is what's specific to *Redis*, and what's specific to *this* codebase.

## 1. What Redis actually is, concretely

Not a library, not an in-process dictionary: **a separate server process**, already running in this project as its own container (`docker-compose.yml`, `redis:7-alpine`, port 6379). The app talks to it over TCP, the same way it talks to Postgres.

That framing matters more than it sounds, because it immediately explains most of Redis's behaviour:

- **Every operation is a network round trip.** A cache hit is not free: ~0.1–0.5ms for the round trip on localhost, plus deserializing the payload back into Python objects in your own process. That's the cost any caching decision has to beat — see the sub-point below, because it's smaller than it first appears.
- **It's shared between every app instance,** which is the entire point. A Python `dict` or `@lru_cache` lives in one process; run two uvicorn workers and you have two independent caches, two independent rate-limit counters, and no lock at all. This is the same "stateless app tier, stateful data tier" split described in `docs/db/concurrency-basics.md` §2 — Redis is data tier.
- **The data lives in RAM.** That's where the speed comes from, and also why everything in it should be treated as *losable*. Redis can persist to disk, but a cache is not a database, and nothing that matters should exist only in Redis.
- **It's effectively single-threaded** for command execution. Commands from all clients are processed one at a time, in arrival order. This is not a limitation to work around — it's the property that makes the rest of this document work, and §7 depends on it entirely.

### The mental model to drop: "database = disk, Redis = RAM"

This is the intuitive reason to reach for a cache, and it's mostly wrong. **Postgres serves from RAM too.** A **hot table** — one read often enough that its pages stay resident in `shared_buffers` and the OS page cache instead of being evicted (see `indexes-basics.md`'s `shared_buffers` entry) — means a repeated indexed query typically never touches a disk either. So a cache hit is usually *RAM instead of RAM*, not RAM instead of disk.

> **The one sentence to keep:** a cache doesn't save you a trip to RAM instead of disk — a hot table already made that trip for you, for free, before Redis ever entered the picture. What a cache saves is *redoing the work* — the parsing, the JOIN, the aggregate, the sort — that turns those already-in-RAM rows into an answer, every single request, indexes or not.

What a cache actually eliminates is the **work**, not the storage medium. Lined up against each other:

| A cache hit pays | The Postgres query pays |
|---|---|
| network round trip | network round trip |
| deserialize JSON → Python | parse + plan the SQL |
| | **execute: joins, aggregates, sorting** |
| | serialize result rows |

Only that bolded row is what caching removes. Which tells you exactly when a cache earns its place — and when it doesn't:

- **`list_filtered` (`app/property/repository.py:57`) is a good candidate.** It LEFT JOINs an aggregate subquery over every review in the system, computes a Bayesian score per property, and sorts by that score, on every request. That's genuine CPU work redone from scratch each time.
- **A primary-key lookup on a hot table is a bad one.** Execution there is already ~0.1ms. You'd swap a 0.6ms query for a 0.4ms cache hit, and pay for it with a staleness window and an invalidation bug surface. Nothing measurable gained.

So the question is never "is RAM faster than a database." It's: **is this query's execution expensive enough to beat a round trip plus deserialization — often enough (§5's hit rate) to be worth the staleness?**

## 2. What's already wired up here

Very little, deliberately:

```python
# app/core/redis_client.py -- the entire file
@lru_cache
def get_redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url)
```

- `redis>=5.2` is in `pyproject.toml`; `redis.asyncio` is the async client, so every call is `await`ed like the SQLAlchemy ones.
- `redis_url` is in `Settings` (`app/core/config.py:14`), pointing at `localhost:6379` for host-run dev and overridden to `redis://redis:6379/0` in `docker-compose.yml` when the app runs in a container — same host-vs-container split as `database_url`.
- `@lru_cache` makes this a singleton, same trick as `get_settings()` and `get_engine()` — see the sub-point below, because the reason is load-bearing rather than cosmetic.
- Its only consumer today is the `/health` ping (`app/core/router.py:29-33`).

So Epic 5 is genuinely the first domain use. There is nothing to un-learn.

### One client, one pool — *not* one connection

`Redis.from_url()` doesn't open a connection. It builds a **`ConnectionPool`** and returns a client that borrows from it:

```
command → pool.get_connection() → send bytes → read reply → pool.release()
```

Connections are created **lazily, on demand**. Issue commands one after another and the pool creates exactly one connection and reuses it forever — which is why it currently looks like "one connection." Issue 50 concurrently and the pool opens up to 50, because a single TCP connection can't carry two in-flight commands and still know which reply belongs to which. **Connection count tracks peak concurrent commands** — not requests, not workers.

**Why `@lru_cache` is doing real work here.** Without it, every call to `get_redis_client()` builds a *new pool*. Pools don't share connections with each other, so a call per request means a pool per request, each opening its own connections and none of them ever reused — climbing toward Redis's `maxclients` (default 10000) until it starts refusing new ones. The decorator is what makes it one pool for the process's lifetime.

**Where the Postgres parallel stops.** `get_engine()` (`app/core/db.py:15`) is the same shape — an engine holding a pool. But Postgres has sessions and transactions, so `db.py` needs a second layer: `get_db()`/`get_uow()` hand out a *request-scoped* `AsyncSession`, because a transaction is stateful and must never be shared across requests. Redis has no equivalent. Commands are independent — no session, no transaction spanning them by default — so the shared client is used directly from anywhere, with no per-request object to manage and no dependency to write.

**Three things that will matter later:**

- **The pool is unbounded by default** (`max_connections=None`). Irrelevant while only `/health` touches Redis; BOOK-42 puts a Redis command on *every request*, which turns `max_connections` into a real decision.
- **Async connections are bound to the event loop that created them.** One loop in the app, so it never comes up there. But `scripts/verify_*.py` call `asyncio.run()`, and an `@lru_cache`d client outlives that call — a second `asyncio.run()` in the same process can reuse connections attached to a closed loop and fail. Worth knowing before writing BOOK-88's benchmark script.
- **Nothing closes it.** There's no `aclose()` anywhere; the pool dies with the process. Harmless today; a `lifespan` handler is the tidy fix.

**Poking at it directly** — worth doing before writing any code:

```bash
docker compose up -d redis
docker compose exec redis redis-cli
```

Then `SET foo bar`, `GET foo`, `TTL foo`, `KEYS *` (interactively only — see §9), `MONITOR` to watch every command the app sends in real time. Ten minutes here is worth more than reading about it.

## 3. The data model: keys, and a few value types

Redis is a key→value store where the key is always a string (really bytes) and the value is one of a handful of types. You need four of them:

| Type | What it is | Used in Epic 5 for |
|---|---|---|
| String | Bytes. A JSON blob, a number, anything | The cached listing payload (BOOK-40); the lock token (BOOK-43) |
| Integer | Still a string, but `INCR`/`DECR` operate on it atomically | Rate-limit counters (BOOK-42) |
| Hash | A map of field→value under one key | Optional — grouping related fields without JSON |
| Sorted set | Members with scores, range-queryable by score | Sliding-window rate limiting (BOOK-42), where the score is a timestamp |

The commands that cover almost everything in this epic:

```
SET key value                  store
SET key value EX 60            store, expire in 60 seconds
SET key value NX PX 5000       store ONLY if absent, expire in 5000ms  <- the lock (BOOK-43)
GET key                        fetch (nil if absent or expired)
DEL key                        remove
INCR key                       atomically +1, returns the new value     <- the counter (BOOK-42)
EXPIRE key 60                  set a TTL on an existing key
TTL key                        seconds left (-1 = no TTL, -2 = gone)
SCAN cursor MATCH pattern      iterate keys, incrementally
```

**Everything is bytes.** `GET` returns `b'...'`, not a `str` and definitely not a dict. You serialize on the way in and deserialize on the way out — for BOOK-40 that means `json.dumps` of the `PropertyListRead` payload, which is also why that ticket says to cache the *serialized schema*, not ORM objects: `Property` instances there carry non-persisted attributes attached in the service (`cover_photo`, `rating_avg`, `review_count`) and are bound to a `Session` that's gone by the time you'd read them back.

(The client can be constructed with `decode_responses=True` to hand back `str` instead of `bytes`. Convenient, but it's a global choice on a shared singleton — decide once, in `redis_client.py`, rather than per call site.)

## 4. TTL is the single most important concept here

Every key can carry an expiry. When it passes, the key is gone — no job to run, no cleanup to write.

This is what makes Redis a *cache* rather than a second database, and it's doing more work than it appears to:

- **It bounds staleness.** A cached value is a copy of something that can change. A 60-second TTL is a promise that the copy is never more than 60 seconds behind — without writing a single line of invalidation logic. That's why BOOK-40 can ship usefully with no invalidation at all, and why BOOK-41's honest baseline is "TTL only."
- **It bounds memory.** Keys clean themselves up. Nothing accumulates forever.
- **It's a safety net under invalidation.** However clever BOOK-41's scheme is, some path will eventually be missed. The TTL is the backstop that makes a missed invalidation a temporary wrong answer instead of a permanent one.

Choosing a TTL is therefore a product decision, not a technical one: *how stale is this answer allowed to be?* A property listing that's 30 seconds out of date is fine. A payment status that's 30 seconds out of date is not.

Contrast with `IdempotencyKey.expires_at` (`app/reservation/models.py:84`): that's a ~24h window on a **Postgres** row, and `idempotency-basics.md` §4 explicitly warns against reaching for Redis's short TTLs there just because Redis is in the stack. Idempotency keys must survive a restart; cache entries needn't. Different requirement, different store.

## 5. Cache-aside, and the two failure modes it has

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

**This is the real argument to weigh in BOOK-40's open question.** Every dimension added to a cache key multiplies the key space: `city × guests × min_price × max_price × sort × limit × offset` is already large, and adding `check_in`/`check_out` multiplies it by every date range anyone might type. A key space much larger than your traffic means nearly every lookup is a miss on a key no one will ever request twice — a cache that only ever pays costs. Hit rate, not elegance, is what should decide that ticket.

Two more properties worth naming:

- **Only requested data is ever cached.** Nothing is pre-warmed, so the cache stays proportional to real traffic.
- **A cold cache means every request is a miss** — plus the write. The first requests after a deploy or a Redis restart are *slower* than no cache at all. Fine at this scale; the reason big systems care is the thundering herd below.

**Caching "nothing" is a decision.** A filter combination matching zero properties is still an answer, and re-deriving it costs the same query as any other. But if a miss is represented as "no key present," you can't tell "not cached yet" apart from "cached, and the answer is an empty list" — so every request for that filter re-runs the query, forever. Storing an explicit empty value (**negative caching**) fixes it, and incidentally closes a cheap denial-of-service: hammering a nonsense filter otherwise bypasses the cache entirely by construction. Decide it deliberately in BOOK-40 rather than inheriting whichever behaviour falls out of `if value is not None`.

**Failure mode 1: the stale window.** Between a write to Postgres and the cache entry expiring or being invalidated, reads return the old answer. This is not a bug to eliminate — it's the trade being made. It just has to be a *known* size (§4) rather than an unbounded one.

**Failure mode 2: the thundering herd (a.k.a. cache stampede).** A popular key expires. Fifty concurrent requests all miss at the same instant, all run the same expensive query, and all write the same value back. The cache made the worst case *worse*, because the misses now cluster instead of being spread out. Mitigations are staggered TTLs (add jitter), or letting one request rebuild while others serve the stale value. Not required for BOOK-40 at this traffic level — worth recognizing by name, because the symptom (periodic DB load spikes at regular intervals) is otherwise baffling.

## 6. Cache invalidation — the hard half

> "There are only two hard things in Computer Science: cache invalidation and naming things." — Phil Karlton

Quoted often enough to have become noise, but the first half is genuinely earned. Worth understanding *why* before BOOK-41 makes you live it.

**The root problem: a cache duplicates state.** Postgres holds the truth; Redis holds a copy. The instant the truth changes, the copy is a lie — and Redis has no idea. Nothing in Redis knows that `bookit:properties:list:<hash>` was derived from a query over the `properties` table, let alone that someone just soft-deleted a row it depended on. There is no foreign key, no trigger, no notification. The application is the only thing that can connect those two facts, and only if someone remembered to write the code.

### Three strategies, in increasing order of effort

**1. Expiry — let it be wrong, briefly.** No invalidation code at all; the TTL (§4) handles it. This is a real strategy, not a cop-out: it's zero lines, it cannot leak a bug, and the staleness is bounded by a number you chose deliberately. Most caches should start here and only move on when the staleness is demonstrably a problem.

**2. Explicit deletion — `DEL` the key when the underlying data changes.** Precise and immediate, and the source of essentially every cache-invalidation bug ever written, for one reason: **you have to find every write path.** Miss one and that key stays wrong until its TTL — silently, with no error anywhere. In this codebase the writes that change a cached property listing are spread across `create_property` / `update_property` / `delete_property`, three separate photo mutations, and review creation (which shifts that property's rating *and*, through the global mean `C`, every other property's ranking score). That's seven-plus call sites today, and an eighth added a year from now by someone who never read this file.

**3. Versioning — make old keys unreachable instead of deleting them.** Embed a generation counter in every key (`bookit:v7:properties:...`). A write bumps the counter; every key built from the previous one is instantly orphaned and expires on its own. Never misses a path, needs no key enumeration, requires no `SCAN` — and over-invalidates enormously, since one property edit cold-starts the entire listing cache.

BOOK-41 has to choose among these (plus prefix-`SCAN`, which is really (2) done blindly). This doc's opinion: **(1) until it demonstrably hurts, then (3)** — because (2)'s failure mode is silent and permanent, while (3)'s is merely wasteful. A cache that's occasionally slower is a much better failure than a cache that's occasionally wrong forever.

### Two things that hold whichever you pick

**Invalidate *after* the commit, never before.** The write happens inside the UoW transaction, which commits in `__aexit__` (`app/core/unit_of_work.py:29`). Delete the cache key mid-service and a concurrent read can miss, re-query, and repopulate the cache from a transaction that then *rolls back* — leaving a cached value that never existed in the database. Ordering here isn't a nicety.

**It can never be atomic with Postgres.** Two systems, two connections, no shared transaction. A crash between "Postgres committed" and "Redis deleted" leaves a stale entry, and no amount of reordering fixes it — reordering only changes which side loses. This is precisely why (1) still matters after you've implemented (2) or (3): **the TTL is what makes every invalidation failure temporary instead of permanent.**

### Delete, don't update

On a write it's tempting to recompute the new value and `SET` it, saving the next reader a miss. Resist. Two concurrent writers can interleave their compute-and-set steps and leave the *older* value in the cache — with a fresh TTL, so nothing corrects it for a full cycle. Deletion has no such race: it's idempotent, and the worst case is that the next read pays for a miss and repopulates from the current truth. **Invalidation should be self-healing, not clever.**

## 7. Redis as a counter — and why atomicity is the whole point

BOOK-42 uses Redis for rate limiting, which is not caching at all. The mechanism:

```
current = INCR ratelimit:<client>     # returns the new value
if current == 1: EXPIRE ratelimit:<client> <window>
if current > limit: reject with 429
```

The critical detail: **`INCR` returns the new value**, and because Redis executes commands one at a time (§1), that value is unique to this caller. Two simultaneous requests get 5 and 6 — never both 5.

Doing it "the obvious way" instead:

```python
current = await redis.get(key)      # both read 4
await redis.set(key, current + 1)   # both write 5
```

...is a read-modify-write race, and it's the *exact* same bug class the `EXCLUDE` constraint exists to prevent for bookings (`docs/db/concurrency-basics.md`): two callers both observe a state before either has acted on it. The lesson from BOOK-35 transfers directly — **let the datastore do the check-and-act in one operation**, don't reconstruct it in Python.

When one command isn't enough (token bucket, or "increment and set TTL" as a single unit), Redis runs **Lua scripts** server-side, atomically, as one command. That's the escape hatch, and it's why `INCR`-then-`EXPIRE` above is written to tolerate the gap rather than assume it can't be interrupted.

## 8. Redis as a lock — and why it's the weakest of the three

BOOK-43's role. The mechanism is `SET key <unique-token> NX PX <ttl>`:

- `NX` = only set if the key doesn't exist. Succeeds for exactly one caller; everyone else gets nil. That's the mutual exclusion.
- `PX <ttl>` = auto-expire, so a holder that crashes doesn't wedge the lock forever.
- The `<unique-token>` matters: release must be a Lua compare-and-delete ("delete only if the value is still *my* token"), never a bare `DEL`. Otherwise a slow holder whose TTL expired can delete a lock that a *different* holder now legitimately owns.

**And it still isn't a correctness guarantee.** Two ways it silently fails:

1. Holder A's TTL expires while A is still working (a GC pause, a slow query). Redis hands the lock to B. Now A and B both believe they hold it.
2. A failover promotes a replica that hadn't yet received A's `SET NX`. B acquires the "same" lock from the new primary.

This is the Redlock debate (Kleppmann vs. Antirez) in two bullets. The takeaway: **a lock that can be lost without the holder noticing is a performance optimization, not a safety property.** Which is precisely why BOOK-43's description says booking correctness must keep resting on the `EXCLUDE` constraint (`app/reservation/models.py:50-56`) and the existing `pg_advisory_xact_lock` (`app/reservation/service.py:134-137`), with Redis as at most a third, advisory layer.

Worth internalizing as the general rule, since it's the one people get wrong: **use a distributed lock to avoid unnecessary work, never to guarantee correctness.** If correctness depends on the lock, the design is already wrong.

## 9. Operational facts that will bite otherwise

- **Eviction.** When Redis hits `maxmemory`, its policy decides what happens. Default in many builds is `noeviction` — meaning **writes start failing** rather than old keys being dropped. For a pure cache you want `allkeys-lru`. Not configured in this project's `docker-compose.yml` at all, which is fine at dev scale and a real decision at any other.
- **Persistence.** Redis can snapshot (RDB) or log (AOF) to disk, but nothing here mounts a volume for it (unlike `pgdata` for Postgres) — so a container restart empties it. That's *correct* for a cache and a rate limiter. It's the reason `idempotency-basics.md` puts idempotency keys in Postgres instead.
- **Databases 0–15.** `redis://localhost:6379/0` — that trailing `/0` selects a numbered logical database. They're a legacy feature, not real isolation (a `FLUSHALL` crosses all of them). Prefer key prefixes.
- **Key naming.** Colon-separated namespaces by convention: `bookit:properties:list:<hash>`, `bookit:ratelimit:login:<ip>`. There is no schema and no enforcement — the prefix is the only structure you'll ever have, so pick it deliberately and write it down in one place.
- **`KEYS` blocks the server.** It scans the entire keyspace, and since Redis is single-threaded (§1), everything else waits. It's fine in `redis-cli` while poking around; it must never appear in application code. `SCAN` is the incremental, non-blocking alternative — which is why BOOK-41 lists prefix-scanning as an option with a caveat attached.
- **Redis being down must not take the app down.** BOOK-40 and BOOK-42 answer this *differently* on purpose: a cache that's unreachable should fall through to Postgres and log (degraded, still correct), while a rate limiter that's unreachable has to choose between failing open (no protection, exactly when things are already going wrong) and failing closed (rejecting real traffic). Note `/health` currently reports Redis as required (`app/core/router.py:35`) — that's a separate claim from what the request path actually does, and the two should agree.

## 10. Where the code goes in this project

Following the module layout in `app/README.md` and `app/core/README.md`:

- **The Redis client and a generic cache helper belong in `core/`** — cross-cutting infrastructure with no domain knowledge, alongside `db.py` and `unit_of_work.py`. A helper that knows what a `Property` is has been written in the wrong place.
- **Cache-aside reads go in `service.py`**, not `repository.py`. Repositories are pure data access; a repository that sometimes doesn't hit the database is no longer that. Not `router.py` either — caching is behaviour, not HTTP.
- **Invalidation is a service concern too**, and must happen *after* the transaction commits. The UoW commits in `__aexit__` (`app/core/unit_of_work.py:29`), so invalidating mid-service lets a concurrent read repopulate the cache from a transaction that then rolls back. There's no post-commit hook today — BOOK-41 has to add one or call out after the UoW exits.
- **Rate limiting is middleware**, attached in `create_app()` (`app/main.py`) next to the CORS middleware — it applies across modules and runs before routing.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Redis | A separate in-memory key→value server, shared by all app instances, reached over TCP — data tier, not a library |
| Single-threaded | Commands execute one at a time in arrival order; this is what makes `INCR` and `SET NX` atomic, and what makes `KEYS` dangerous |
| TTL | Per-key expiry. Bounds staleness, bounds memory, and backstops any missed invalidation — the core of why a cache is safe |
| Cache-aside | Check cache → on miss, query DB → write result back with a TTL. App owns the logic; Redis knows nothing about Postgres |
| Cache hit / miss | `GET` returned a value / returned nil. "Expired" and "never existed" are indistinguishable from the app's side |
| Hit rate | Hits ÷ lookups. A miss costs *more* than no cache (round trip + query + write-back), so a low hit rate makes things slower |
| What a cache actually saves | Query *execution* (joins, aggregates, sorts) — not disk access. Postgres serves from RAM too |
| Negative caching | Storing an explicit empty result, so a zero-match filter doesn't re-query forever and can't be used to bypass the cache |
| Stale window | The gap between a DB write and the cached copy catching up — the trade being made, acceptable only when its size is known |
| Thundering herd | A hot key expires and N concurrent requests all rebuild it at once. Mitigated with TTL jitter or single-flight rebuild |
| Cache invalidation | Making a copy stop being wrong. Three strategies: expiry (§4), explicit `DEL`, or key versioning — see §6 |
| Invalidation's core problem | Redis has no idea its copy came from Postgres. Only application code connects those facts, and only if someone wrote it |
| Versioning / generation key | A counter embedded in every key; bumping it orphans all old keys at once. Never misses a write path, over-invalidates hugely |
| Delete, don't update | Recomputing-and-`SET`ting on write lets two writers interleave and leave the *older* value cached. `DEL` is self-healing |
| Post-commit ordering | Invalidate after the UoW commits — earlier, and a concurrent read can repopulate the cache from a transaction that rolls back |
| `INCR` | Atomic +1 returning the new value. Replaces a `GET`-then-`SET` race, same bug class the `EXCLUDE` constraint prevents for bookings |
| `SET NX PX` | Set-if-absent with an expiry — the distributed-lock primitive. Release via Lua compare-and-delete, never bare `DEL` |
| Redlock critique | A Redis lock can be lost to TTL expiry or failover without the holder noticing → optimization, never a correctness boundary |
| Eviction policy | What Redis drops at `maxmemory`. `allkeys-lru` for a cache; the `noeviction` default makes *writes fail* instead |
| `KEYS` vs `SCAN` | `KEYS` blocks the single-threaded server across the whole keyspace — cli only. `SCAN` iterates incrementally |
| Connection pool | `Redis.from_url` returns a client over a *pool*, not a connection. Connections open lazily; the count tracks peak *concurrent* commands |
| `@lru_cache` on the client | Prevents a new pool per call. A pool per request would climb toward Redis's `maxclients` and never reuse anything |
| No Redis "session" | Unlike Postgres, commands are independent — no request-scoped object, no `get_redis()` dependency, just the shared client |
| Redis's three roles here | Cache (BOOK-40/41), counter (BOOK-42), lock (BOOK-43) — same server, three unrelated jobs |
