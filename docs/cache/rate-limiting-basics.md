# Redis as a counter — rate limiting (BOOK-42)

Split out of `redis-basics.md` — read that first §1 for why Redis being single-threaded matters; this file depends on it entirely. This is not caching — it's a different one of Redis's three jobs in this project (see `redis-basics.md`'s closing summary).

## Redis as a counter — and why atomicity is the whole point

BOOK-42 uses Redis for rate limiting, which is not caching at all. The mechanism:

```
current = INCR ratelimit:<client>     # returns the new value
if current == 1: EXPIRE ratelimit:<client> <window>
if current > limit: reject with 429
```

The critical detail: **`INCR` returns the new value**, and because Redis executes commands one at a time (`redis-basics.md` §1), that value is unique to this caller. Two simultaneous requests get 5 and 6 — never both 5.

Doing it "the obvious way" instead:

```python
current = await redis.get(key)      # both read 4
await redis.set(key, current + 1)   # both write 5
```

...is a read-modify-write race, and it's the *exact* same bug class the `EXCLUDE` constraint exists to prevent for bookings (`docs/db/concurrency-basics.md`): two callers both observe a state before either has acted on it. The lesson from BOOK-35 transfers directly — **let the datastore do the check-and-act in one operation**, don't reconstruct it in Python.

When one command isn't enough (token bucket, or "increment and set TTL" as a single unit), Redis runs **Lua scripts** server-side, atomically, as one command. That's the escape hatch, and it's why `INCR`-then-`EXPIRE` above is written to tolerate the gap rather than assume it can't be interrupted.

**Where this lands in the app:** rate limiting is middleware, attached in `create_app()` (`app/main.py`) next to the CORS middleware — it applies across modules and runs before routing, unlike cache-aside (`cache-aside-basics.md`) which is a `service.py` concern for one endpoint at a time.

**Fail open or fail closed?** Worth deciding deliberately, not by accident: if Redis is unreachable, does the rate limiter let all traffic through (fail open — no protection, exactly when things are already going wrong) or reject everything (fail closed — real users blocked by an infra hiccup)? `redis-basics.md`'s operational notes cover why this answer is allowed to differ from cache-aside's "always degrade to Postgres" default.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `INCR` | Atomic +1 returning the new value. Replaces a `GET`-then-`SET` race, same bug class the `EXCLUDE` constraint prevents for bookings |
| Lua script | How Redis makes a multi-step operation atomic when one command isn't enough (e.g. increment + conditional expire) |
| Fail open vs. fail closed | What a rate limiter does when Redis is unreachable — let traffic through unprotected, or reject it. A deliberate choice, not a default |
