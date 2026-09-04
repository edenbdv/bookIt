# Redis & caching basics (Epic 5 / BOOK-7)

Written as theory before touching anything, same approach as `exclude-constraint-basics.md` and `idempotency-basics.md` — updated in place once the actual BOOK-40/41/42/43 work happens.

Assumes the general idea of a cache is already familiar ("keep a copy of an expensive answer somewhere fast"). What follows is what's specific to *Redis*, and what's specific to *this* codebase — the parts shared across all four tickets below. Each ticket's own mechanics live in their own file:

- **[`cache-aside-basics.md`](cache-aside-basics.md)** — BOOK-40: reading through a cache, TTL, hit rate, when it's worth it
- **[`cache-invalidation-basics.md`](cache-invalidation-basics.md)** — BOOK-41: keeping a cached value from going stale
- **[`rate-limiting-basics.md`](rate-limiting-basics.md)** — BOOK-42: Redis as an atomic counter
- **[`distributed-lock-basics.md`](distributed-lock-basics.md)** — BOOK-43: Redis as a (weak) lock

## 1. What Redis actually is, concretely

Not a library, not an in-process dictionary: **a separate server process**, already running in this project as its own container (`docker-compose.yml`, `redis:7-alpine`, port 6379). The app talks to it over TCP, the same way it talks to Postgres.

That framing matters more than it sounds, because it immediately explains most of Redis's behaviour:

- **Every operation is a network round trip.** A cache hit is not free: ~0.1–0.5ms for the round trip on localhost, plus deserializing the payload back into Python objects in your own process. That's the cost any caching decision has to beat — see the sub-point below, because it's smaller than it first appears.
- **It's shared between every app instance,** which is the entire point. A Python `dict` or `@lru_cache` lives in one process; run two uvicorn workers and you have two independent caches, two independent rate-limit counters, and no lock at all. This is the same "stateless app tier, stateful data tier" split described in `docs/db/concurrency-basics.md` §2 — Redis is data tier.
- **The data lives in RAM.** That's where the speed comes from, and also why everything in it should be treated as *losable*. Redis can persist to disk, but a cache is not a database, and nothing that matters should exist only in Redis.
- **It's effectively single-threaded** for command execution. Commands from all clients are processed one at a time, in arrival order. This is not a limitation to work around — it's the property that makes the rest of this document work, and `rate-limiting-basics.md` depends on it entirely.

### The mental model to drop: "database = disk, Redis = RAM"

This is the intuitive reason to reach for a cache, and it's mostly wrong. **Postgres serves from RAM too.** A **hot table** — one read often enough that its pages stay resident in `shared_buffers` and the OS page cache instead of being evicted (see `docs/db/indexes-basics.md`'s `shared_buffers`/hot table entries) — means a repeated indexed query typically never touches a disk either. So a cache hit is usually *RAM instead of RAM*, not RAM instead of disk.

> **The one sentence to keep:** a cache doesn't save you a trip to RAM instead of disk — a hot table already made that trip for you, for free, before Redis ever entered the picture. What a cache saves is *redoing the work* — the parsing, the JOIN, the aggregate, the sort — that turns those already-in-RAM rows into an answer, every single request, indexes or not.

What a cache actually eliminates is the **work**, not the storage medium. Lined up against each other:

| A cache hit pays | The Postgres query pays |
|---|---|
| network round trip | network round trip |
| deserialize JSON → Python | parse + plan the SQL |
| | **execute: joins, aggregates, sorting** |
| | serialize result rows |

Only that bolded row is what caching removes. Which tells you exactly when a cache earns its place — and when it doesn't:

- **`list_filtered` (`app/property/repository.py:57`) looked like a good candidate on reasoning alone — BOOK-88 measured it and found a more specific picture.** Cost (~70-80ms at realistic scale) turned out concentrated almost entirely in two review-aggregation computations that ignore every filter, not in `list_filtered` as a whole: `_global_mean_rating()` (one global, slow-changing scalar — a real caching target, now BOOK-40's actual scope) and `_review_stats_subquery()` (scales with total system data, not any cache key — a query/index fix, BOOK-92, not a caching one). Caching the whole listing response would have worked but treated the symptom rather than the measured cause. Full numbers: BOOK-88's findings comment; plan walkthrough: `docs/db/explain-analyze-basics.md`.
- **A primary-key lookup on a hot table is a bad one.** Execution there is already ~0.1ms. You'd swap a 0.6ms query for a 0.4ms cache hit, and pay for it with a staleness window and an invalidation bug surface. Nothing measurable gained.

So the question is never "is RAM faster than a database." It's: **is this query's execution expensive enough to beat a round trip plus deserialization — often enough (`cache-aside-basics.md`'s hit rate) to be worth the staleness?** That's exactly the question BOOK-88 measures before BOOK-40 writes any code.

The whole section in one table:

| | `shared_buffers` (Postgres's RAM cache) | Redis |
|---|---|---|
| Main purpose | Make DB access faster | Avoid DB work entirely |
| Stores | DB pages (raw heap/index bytes) | Application-level values/results |
| Persistent source of truth | ✅ (it's a cache *of* the durable table on disk) | Usually ❌ (§4 below — a container restart empties it, by design) |
| Query execution (parse/plan/join/aggregate/sort) | ✅ still happens, every time | ❌ never — it's a key lookup, nothing more |
| Very fast key→value lookup | — (still a B-tree walk — see `docs/db/indexes-basics.md`) | ✅ O(1) hash lookup |
| Handles complex queries | ✅ | ❌ |
| Typical use here | Every request, transparently, no app code involved | Deliberately chosen per-endpoint (BOOK-40) for the expensive ones |

**In one sentence:** `shared_buffers` makes **Postgres** faster; Redis lets you **skip Postgres entirely** for an answer you've already computed once.

## 2. What's already wired up here

Very little, deliberately:

```python
# app/core/cache/redis_client.py -- the entire file
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

**Where the Postgres parallel stops.** `get_engine()` (`app/core/db/engine.py:15`) is the same shape — an engine holding a pool. But Postgres has sessions and transactions, so `db.py` needs a second layer: `get_db()`/`get_uow()` hand out a *request-scoped* `AsyncSession`, because a transaction is stateful and must never be shared across requests. Redis has no equivalent. Commands are independent — no session, no transaction spanning them by default — so the shared client is used directly from anywhere, with no per-request object to manage and no dependency to write.

**Three things that will matter later:**

- **The pool is unbounded by default** (`max_connections=None`). Irrelevant while only `/health` touches Redis; BOOK-42 puts a Redis command on *every request*, which turns `max_connections` into a real decision.
- **Async connections are bound to the event loop that created them.** One loop in the app, so it never comes up there. But `scripts/verify_*.py` call `asyncio.run()`, and an `@lru_cache`d client outlives that call — a second `asyncio.run()` in the same process can reuse connections attached to a closed loop and fail. Worth knowing before writing BOOK-88's benchmark script.
- **Nothing closes it.** There's no `aclose()` anywhere; the pool dies with the process. Harmless today; a `lifespan` handler is the tidy fix.

**Poking at it directly** — worth doing before writing any code:

```bash
docker compose up -d redis
docker compose exec redis redis-cli
```

Then `SET foo bar`, `GET foo`, `TTL foo`, `KEYS *` (interactively only — see §4 below), `MONITOR` to watch every command the app sends in real time. Ten minutes here is worth more than reading about it.

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

**Everything is bytes.** `GET` returns `b'...'`, not a `str` and definitely not a dict. You serialize on the way in and deserialize on the way out — for BOOK-40 that means `json.dumps` of the `PropertyListRead` payload, which is also why that ticket says to cache the *serialized schema*, not ORM objects: `Property` instances there carry non-persisted attributes attached in the service (`cover_photo`, `rating_avg`, `review_count`) and are bound to a `Session` that's gone by the time you'd read them back. Full pattern in `cache-aside-basics.md`.

(The client can be constructed with `decode_responses=True` to hand back `str` instead of `bytes`. Convenient, but it's a global choice on a shared singleton — decide once, in `redis_client.py`, rather than per call site.)

## 4. Operational facts that will bite otherwise

- **Eviction.** When Redis hits `maxmemory`, its policy decides what happens. Default in many builds is `noeviction` — meaning **writes start failing** rather than old keys being dropped. For a pure cache you want `allkeys-lru`. Not configured in this project's `docker-compose.yml` at all, which is fine at dev scale and a real decision at any other.
- **Persistence.** Redis can snapshot (RDB) or log (AOF) to disk, but nothing here mounts a volume for it (unlike `pgdata` for Postgres) — so a container restart empties it. That's *correct* for a cache and a rate limiter. It's the reason `idempotency-basics.md` puts idempotency keys in Postgres instead.
- **Databases 0–15.** `redis://localhost:6379/0` — that trailing `/0` selects a numbered logical database. They're a legacy feature, not real isolation (a `FLUSHALL` crosses all of them). Prefer key prefixes.
- **Key naming.** Colon-separated namespaces by convention: `bookit:properties:list:<hash>`, `bookit:ratelimit:login:<ip>`. There is no schema and no enforcement — the prefix is the only structure you'll ever have, so pick it deliberately and write it down in one place.
- **`KEYS` blocks the server.** It scans the entire keyspace, and since Redis is single-threaded (§1), everything else waits. It's fine in `redis-cli` while poking around; it must never appear in application code. `SCAN` is the incremental, non-blocking alternative — which is why `cache-invalidation-basics.md`'s prefix-scanning option carries that caveat too.
- **Redis being down must not take the app down.** BOOK-40 and BOOK-42 answer this *differently* on purpose: a cache that's unreachable should fall through to Postgres and log (degraded, still correct — see `cache-aside-basics.md`), while a rate limiter that's unreachable has to choose between failing open and failing closed (`rate-limiting-basics.md`). Note `/health` currently reports Redis as required (`app/core/router.py:35`) — that's a separate claim from what the request path actually does, and the two should agree.

## 5. Where the code goes in this project

Following the module layout in `app/README.md` and `app/core/README.md`:

- **The Redis client and a generic cache helper belong in `core/`** — cross-cutting infrastructure with no domain knowledge, alongside `db.py` and `unit_of_work.py`. A helper that knows what a `Property` is has been written in the wrong place.
- **Cache-aside reads go in `service.py`**, not `repository.py`. Repositories are pure data access; a repository that sometimes doesn't hit the database is no longer that. Not `router.py` either — caching is behaviour, not HTTP. Details: `cache-aside-basics.md`.
- **Invalidation is a service concern too**, and must happen *after* the transaction commits. The UoW commits in `__aexit__` (`app/core/db/unit_of_work.py:29`), so invalidating mid-service lets a concurrent read repopulate the cache from a transaction that then rolls back. There's no post-commit hook today — BOOK-41 has to add one or call out after the UoW exits. Details: `cache-invalidation-basics.md`.
- **Rate limiting is middleware**, attached in `create_app()` (`app/main.py`) next to the CORS middleware — it applies across modules and runs before routing. Details: `rate-limiting-basics.md`.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Redis | A separate in-memory key→value server, shared by all app instances, reached over TCP — data tier, not a library |
| Single-threaded | Commands execute one at a time in arrival order; this is what makes `INCR` and `SET NX` atomic, and what makes `KEYS` dangerous |
| Hot table | A Postgres table read often enough that its pages stay resident in `shared_buffers`/OS cache — removes disk I/O, not query *execution* cost. See `docs/db/indexes-basics.md` |
| What a cache actually saves | Query *execution* (joins, aggregates, sorts) — not disk access. Postgres serves from RAM too |
| Connection pool | `Redis.from_url` returns a client over a *pool*, not a connection. Connections open lazily; the count tracks peak *concurrent* commands |
| `@lru_cache` on the client | Prevents a new pool per call. A pool per request would climb toward Redis's `maxclients` and never reuse anything |
| No Redis "session" | Unlike Postgres, commands are independent — no request-scoped object, no `get_redis()` dependency, just the shared client |
| Eviction policy | What Redis drops at `maxmemory`. `allkeys-lru` for a cache; the `noeviction` default makes *writes fail* instead |
| `KEYS` vs `SCAN` | `KEYS` blocks the single-threaded server across the whole keyspace — cli only. `SCAN` iterates incrementally |
| Redis's three roles here | Cache (`cache-aside-basics.md`/`cache-invalidation-basics.md`), counter (`rate-limiting-basics.md`), lock (`distributed-lock-basics.md`) — same server, three unrelated jobs |
