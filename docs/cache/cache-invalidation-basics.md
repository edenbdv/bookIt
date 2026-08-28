# Cache invalidation — the hard half (BOOK-41)

Split out of `redis-basics.md` — read `cache-aside-basics.md` first (TTL and the cache-aside pattern this builds on). This file is specifically about keeping a cached value from going stale after the underlying data changes.

## Cache invalidation — the hard half

> "There are only two hard things in Computer Science: cache invalidation and naming things." — Phil Karlton

Quoted often enough to have become noise, but the first half is genuinely earned. Worth understanding *why* before BOOK-41 makes you live it.

**The root problem: a cache duplicates state.** Postgres holds the truth; Redis holds a copy. The instant the truth changes, the copy is a lie — and Redis has no idea. Nothing in Redis knows that `bookit:properties:list:<hash>` was derived from a query over the `properties` table, let alone that someone just soft-deleted a row it depended on. There is no foreign key, no trigger, no notification. The application is the only thing that can connect those two facts, and only if someone remembered to write the code.

### Three strategies, in increasing order of effort

**1. Expiry — let it be wrong, briefly.** No invalidation code at all; the TTL (`cache-aside-basics.md` §1) handles it. This is a real strategy, not a cop-out: it's zero lines, it cannot leak a bug, and the staleness is bounded by a number you chose deliberately. Most caches should start here and only move on when the staleness is demonstrably a problem.

**2. Explicit deletion — `DEL` the key when the underlying data changes.** Precise and immediate, and the source of essentially every cache-invalidation bug ever written, for one reason: **you have to find every write path.** Miss one and that key stays wrong until its TTL — silently, with no error anywhere. In this codebase the writes that change a cached property listing are spread across `create_property` / `update_property` / `delete_property`, three separate photo mutations, and review creation (which shifts that property's rating *and*, through the global mean `C`, every other property's ranking score). That's seven-plus call sites today, and an eighth added a year from now by someone who never read this file.

**3. Versioning — make old keys unreachable instead of deleting them.** Embed a generation counter in every key (`bookit:v7:properties:...`). A write bumps the counter; every key built from the previous one is instantly orphaned and expires on its own. Never misses a path, needs no key enumeration, requires no `SCAN` — and over-invalidates enormously, since one property edit cold-starts the entire listing cache.

BOOK-41 has to choose among these (plus prefix-`SCAN`, which is really (2) done blindly — see `redis-basics.md`'s note on why `KEYS`/unbounded `SCAN` are dangerous). This doc's opinion: **(1) until it demonstrably hurts, then (3)** — because (2)'s failure mode is silent and permanent, while (3)'s is merely wasteful. A cache that's occasionally slower is a much better failure than a cache that's occasionally wrong forever.

### Two things that hold whichever you pick

**Invalidate *after* the commit, never before.** The write happens inside the UoW transaction, which commits in `__aexit__` (`app/core/unit_of_work.py:29`). Delete the cache key mid-service and a concurrent read can miss, re-query, and repopulate the cache from a transaction that then *rolls back* — leaving a cached value that never existed in the database. Ordering here isn't a nicety.

**It can never be atomic with Postgres.** Two systems, two connections, no shared transaction. A crash between "Postgres committed" and "Redis deleted" leaves a stale entry, and no amount of reordering fixes it — reordering only changes which side loses. This is precisely why (1) still matters after you've implemented (2) or (3): **the TTL is what makes every invalidation failure temporary instead of permanent.**

### Delete, don't update

On a write it's tempting to recompute the new value and `SET` it, saving the next reader a miss. Resist. Two concurrent writers can interleave their compute-and-set steps and leave the *older* value in the cache — with a fresh TTL, so nothing corrects it for a full cycle. Deletion has no such race: it's idempotent, and the worst case is that the next read pays for a miss and repopulates from the current truth. **Invalidation should be self-healing, not clever.**

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Cache invalidation | Making a copy stop being wrong. Three strategies: expiry (TTL, see `cache-aside-basics.md`), explicit `DEL`, or key versioning |
| Invalidation's core problem | Redis has no idea its copy came from Postgres. Only application code connects those facts, and only if someone wrote it |
| Versioning / generation key | A counter embedded in every key; bumping it orphans all old keys at once. Never misses a write path, over-invalidates hugely |
| Delete, don't update | Recomputing-and-`SET`ting on write lets two writers interleave and leave the *older* value cached. `DEL` is self-healing |
| Post-commit ordering | Invalidate after the UoW commits — earlier, and a concurrent read can repopulate the cache from a transaction that rolls back |
