# Redis as a counter — rate limiting (BOOK-42)

Split out of `redis-basics.md` — read that first §1 for why Redis being single-threaded matters; this file depends on it entirely. This is not caching — it's a different one of Redis's three jobs in this project (see `redis-basics.md`'s closing summary).

## Why Redis, even with a single app instance

Redis isn't only justified once there's more than one server. Even with one process, a hand-rolled in-memory counter (a plain dict) has two problems Redis solves for free:

- **Cleanup.** Every new client creates an entry that has to expire, or the dict grows forever. Redis's `EXPIRE` does this automatically (same TTL mechanism as `cache-aside-basics.md` §1) — no cleanup code to write.
- **Atomicity.** Two concurrent requests from the same client against a Python dict hit the exact `GET`-then-`SET` race described below. `INCR` is atomic by construction.

**The most visible win, though, is once there's more than one app instance.** Two uvicorn workers (or two containers behind a load balancer) each holding their own in-memory dict have two *independent* counters — a client can double its effective limit just by landing on a different instance, and a restart silently wipes whatever was counted. Redis is shared state outside any one process, so every instance checks and updates the *same* count. `bookIt` runs a single `app` service today (`docker-compose.yml`), so this specific win is latent rather than active here — but the cleanup/atomicity wins above hold regardless of instance count.

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

## Algorithm choice: fixed window vs. sliding window log vs. token bucket

The `INCR`/`EXPIRE` pair above is **fixed window** — the simplest of three ways to do this, and BOOK-42 asks to pick one deliberately, not by default.

**Fixed window** (shown above): one Redis round trip per request. It counts requests inside a fixed, clock-aligned bucket (e.g. "this calendar minute"), not the actual trailing N seconds. A client sending `limit` requests in the last second of one window and `limit` more in the first second of the next gets `2 * limit` requests inside roughly two seconds — double the intended rate, right at the boundary. Cheap, imprecise at the edges.

**Sliding window log**:

```
ZADD ratelimit:<client> <now> <now>-<random>            # log this request's timestamp
ZREMRANGEBYSCORE ratelimit:<client> -inf <now-window>    # drop anything older than the window
count = ZCARD ratelimit:<client>
if count > limit: reject
```

A sorted set holds one entry per request, scored by timestamp. `ZCARD` after trimming old entries gives an exact count over the *actual* trailing window — no boundary-doubling. Cost: three commands instead of one, and memory proportional to `limit` (one entry per request currently in the window), not a single integer.

**Token bucket**: a bucket holds up to `capacity` tokens, refilling at a fixed rate; each request consumes one token, rejected only if the bucket is empty. Unlike fixed window, it allows an intentional burst up to `capacity` even after a period of low traffic — "smooth out effort over time" rather than "hard cap per clock tick." It needs a Lua script (the escape hatch above): "how many tokens refilled since last check, then consume one" is several steps — read last-refill timestamp, read current tokens, compute elapsed-based refill, clamp to capacity, decrement, write back — that all have to happen as one atomic unit, or two concurrent requests could both read the same stale token count.

**Which one, and why:** fixed window is the right default here — one command versus three (sliding log) or a Lua script (token bucket), for a boundary imprecision that matters little when limits are generous safety nets, not precise quotas. The one place worth reconsidering is `/auth/login`, where the 2× boundary burst is exactly the gap a credential-stuffing script would exploit — sliding window log's exactness earns its extra cost there specifically, even if fixed window covers everything else.

**Where this lands in the app:** the public-endpoint half of this is middleware, attached in `create_app()` (`app/main.py`) next to the CORS middleware — it applies across modules and runs before routing, unlike cache-aside (`cache-aside-basics.md`) which is a `service.py` concern for one endpoint at a time. The authenticated half is not middleware at all — see the next section for why.

## Client identity: per-IP vs. per-user, and why that's not just a config choice

`ratelimit:<client>` above hides a real decision: what *is* `<client>`?

- **Public endpoints** (`/auth/login`, `/auth/signup`, `/auth/refresh`, `GET /properties*`) — no authenticated identity exists yet at request time, so `<client>` can only be the caller's IP.
- **Everything behind `get_current_user`** (`get-current-user-basics.md`) — standard practice for authenticated APIs (GitHub, Stripe, Twitter/X all do this) is to key by **user id**, not IP. Two legitimate users sharing a NAT'd office IP shouldn't share one limit, and IP alone doesn't stop a single authenticated account being hammered from many rotating IPs.

That split is bigger than "which string to hash into the key" — it decides *where the check can even run*. Middleware attached in `create_app()` executes **before** FastAPI resolves any `Depends(...)`, including `get_current_user` (`dependency-injection-basics.md`). At that point in the request there is no verified user yet — only a raw request. Middleware alone can give IP-keyed limiting; it structurally cannot give user-id-keyed limiting, because the thing it would key on doesn't exist at that layer.

So this is two enforcement points sharing the `INCR`/`EXPIRE` mechanism above, not one:

1. **IP-keyed**, in middleware — public endpoints, runs before routing.
2. **User-id-keyed**, as a dependency (e.g. `Depends(rate_limit_by_user)`) applied to routes that already use `Depends(get_current_user)` — runs *after* identity resolves, keyed on the same `User` the route itself receives.

**Defense in depth, not either/or.** Per-user alone leaves a gap: one account can still push heavy load from a single IP as long as it stays under its own ceiling, and per-user does nothing to slow an attacker spinning up many accounts from one IP (though `/auth/signup` being IP-limited already covers account creation itself). The common production pattern is a per-user limit as the primary, fairness-oriented check, with a coarser per-IP ceiling underneath as a floor — two keys checked in the same dependency, not two separate systems.

**Login's optional per-email key is a third case, with its own hazard.** It catches credential stuffing spread across many IPs against one target account — something per-IP alone would never notice, since each IP only makes a couple of attempts. But the counter must not create a response that differs depending on whether the email is actually registered — the exact "no such email" vs. "wrong password" trap `user-enumeration-basics.md` covers for the login check itself. If only existing accounts accumulate a counter (and so only they can ever hit `429`), the `429` itself becomes the leak; the count has to be maintained, and checked, the same way regardless of whether the email resolves to a real user.

## Client identity, continued: `X-Forwarded-For` and why "it came through my proxy" isn't enough

`request.client.host` is the direct socket peer — the thing that opened the TCP connection to the app. Behind any reverse proxy or load balancer, that's the *proxy's* IP, not the real caller's. Every real user's request would collapse onto one IP, and per-IP limiting becomes meaningless — one heavy user (or the proxy itself) exhausts the shared count for everyone else.

The standard fix is the `X-Forwarded-For` header: the proxy appends the real client IP before forwarding the request, so the app reads that instead of `request.client.host` — *if* the header can be trusted.

**The trap:** `X-Forwarded-For` is a plain HTTP header, and most proxies (nginx's default `$proxy_add_x_forwarded_for`, for instance) **append** to whatever value already arrived rather than replacing it. An attacker hitting the proxy's public address like any normal user can pre-set `X-Forwarded-For: 1.2.3.4` on their own request; the proxy appends its own view of the connection on top, so what reaches the app is `X-Forwarded-For: 1.2.3.4, <attacker's real IP>`. A check like "did this request arrive through my known proxy" (`request.client.host == proxy IP`) *passes* here — the attacker really did go through the real proxy — so that check alone doesn't stop the spoof if the app then blindly trusts the first (leftmost) value in the list.

**The actual fix is one of:**
1. Configure the proxy to **strip** any client-supplied `X-Forwarded-For` and set a fresh one from the real connection (`proxy_set_header X-Forwarded-For $remote_addr;` in nginx, not the default append behavior) — the app then never sees attacker-controlled data in that header at all.
2. If stripping isn't possible (a chain of multiple trusted proxies), know exactly how many hops are trusted and read the IP at that fixed position counting from the *right-hand* end of the list — never the leftmost value, since everything left of your own infrastructure's contribution can be attacker-supplied.

`bookIt` has no proxy in front of `app` yet (`docker-compose.yml` exposes port 8000 directly) — so today `request.client.host` is always the real caller, and this whole question is moot. It becomes live the moment a reverse proxy or load balancer is added; the decision needs to be made then, not assumed safe just because a request "came from the right place."

**Fail open or fail closed?** This is a separate question from client identity — it's about what happens when the app tries to reach Redis to check a counter and Redis simply doesn't answer (down, timeout, network partition). Two options, and Redis being unreachable means you cannot have both at once:

- **Fail open** — treat the failure as "no limit," let the request through as if nothing happened. Users notice nothing. But for as long as Redis is down, there is **no rate limiting at all** — exactly the window an attacker would want to be probing in.
- **Fail closed** — treat the failure as a rejection, refuse the request. Protection holds through the outage, but every legitimate user gets blocked too, purely because of infra they had nothing to do with.

`redis-basics.md`'s operational notes cover why this answer is allowed to differ from cache-aside's "always degrade to Postgres" default — cache-aside has a real fallback (the DB); a rate limiter during a Redis outage does not have an equivalent fallback to degrade to. It also doesn't have to be the same answer for every endpoint — see the decision below.

## Response format: `429`, `Retry-After`, and the `X-RateLimit-*` headers

A rejected request returns `429 Too Many Requests` — not `401`/`403`, which mean "you're not allowed," ever. `429` means "you're allowed, just not right now."

**Required: `Retry-After`**, seconds until the caller can reasonably try again. Without it, a client can only guess when to retry — poll immediately (making the problem worse) or back off arbitrarily.

**Nice-to-have: `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`**, returned on *every* response, not just `429`s — the ceiling, how much of it is left, and when the counter resets. This is what GitHub/Stripe/Twitter return on every API call, letting a well-behaved client see it's approaching its limit and slow down on its own, instead of finding out only by getting rejected.

**Login is the one place to withhold the nice-to-have.** `X-RateLimit-Remaining` on `/auth/login` hands an attacker exact telemetry on how close they are to getting locked out — they can throttle themselves to stay just under the threshold indefinitely, defeating the point of the limit. Public reads and everything else have nothing sensitive to leak by exposing it, so they get the full set.

| Endpoint group | Response headers |
|---|---|
| `POST /auth/login` | `429` + `Retry-After` only — no `X-RateLimit-*` |
| Everything else rate-limited | `429` + `Retry-After` + `X-RateLimit-Limit`/`-Remaining`/`-Reset` |

## Decision: what BOOK-42 actually implements, endpoint by endpoint

Putting every section above together:

| Endpoint group | Algorithm | Key(s) | Redis down |
|---|---|---|---|
| `POST /auth/login` | Sliding window log | per-IP **and** per-email | Fail **closed** |
| `POST /auth/signup`, `POST /auth/refresh` | Fixed window | per-IP | Fail **closed** |
| `GET /properties`, `/properties/cities`, `/properties/{id}`, `/properties/{id}/reviews` | Fixed window | per-IP | Fail **open** |
| Everything behind `get_current_user` | Fixed window | per-user id (+ per-IP floor) | Fail **open** |

**Why login is the odd one out on algorithm and keys:** it's the one place the fixed-window boundary burst (above) and a missing per-email key are actual security gaps, not just extra load — see the two sections above. Everywhere else, fixed window + per-IP (or per-user) is cheap and sufficient.

**Why fail mode splits instead of being uniform:** `/auth/*` is where the limiter *is* the security control — a credential-stuffing script doesn't care that Redis happened to be down, so failing open there hands out the exact protection gap an attacker wants, at the exact moment (an outage) an attacker is most likely to be probing. Public reads and authenticated-route limits, by contrast, exist for fairness and load-shaping, not security — taking down property browsing or every authenticated action because of a Redis blip is a worse outcome than briefly losing a soft cap on request volume. Same reasoning BOOK-40 used for caching (degrade, don't 5xx), applied to the endpoints where the limiter isn't a security boundary.

## Settings: where the numbers live

Every limit above is a `(limit, window_seconds)` pair — `limit` is how many attempts are allowed, `window_seconds` how long that allowance covers. Both belong in `Settings` (`app/core/config.py`), never hardcoded, for the same reason `rating_smoothing_m` does:

```python
# BOOK-77 ranking tunable: ... Environment-overridable so it can be tuned
# without a redeploy -- but note that overriding it per-environment means
# staging and production will order listings differently.
rating_smoothing_m: int = 5
```

That warning transfers directly, and matters *more* here: if dev/staging loosens the limits "so testing isn't annoying," then the exact scenario the limiter exists to catch — a brute-force script hammering `/auth/login` — never gets exercised until production, which is the one environment where finding out the hard way is expensive.

```python
# BOOK-42: rate limiting. (limit, window_seconds) pairs -- see
# docs/cache/rate-limiting-basics.md for the algorithm/key/fail-mode
# decision behind each group. Do not loosen these per-environment: doing
# so means staging never exercises the scenario the limiter exists to
# catch, same warning as rating_smoothing_m above.

# login -- sliding window log, checked per-IP AND per-email
rate_limit_login_ip_limit: int = 10
rate_limit_login_ip_window_seconds: int = 60
rate_limit_login_email_limit: int = 5
rate_limit_login_email_window_seconds: int = 60

# signup, refresh -- fixed window, per-IP
rate_limit_auth_ip_limit: int = 20
rate_limit_auth_ip_window_seconds: int = 60

# public property reads -- fixed window, per-IP, loose
rate_limit_properties_ip_limit: int = 100
rate_limit_properties_ip_window_seconds: int = 60

# authenticated routes -- fixed window, per-user primary + per-IP floor
rate_limit_user_limit: int = 100
rate_limit_user_window_seconds: int = 60
rate_limit_user_ip_floor_limit: int = 300
rate_limit_user_ip_floor_window_seconds: int = 60
```

Eight pairs, not one shared `RATE_LIMIT` constant — each maps directly to one row of the decision table above. Collapsing them into fewer settings would silently re-couple groups that were deliberately split (e.g. login's per-email check has no equivalent anywhere else, and the authenticated floor is intentionally looser than the per-user ceiling it sits under).

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Why Redis, single instance | `EXPIRE` handles cleanup, `INCR` handles atomicity — both real problems even with one server |
| Why Redis, multi-instance | The counter is shared state, not per-process — otherwise each instance has its own limit |
| `INCR` | Atomic +1 returning the new value. Replaces a `GET`-then-`SET` race, same bug class the `EXCLUDE` constraint prevents for bookings |
| Lua script | How Redis makes a multi-step operation atomic when one command isn't enough (e.g. increment + conditional expire) |
| Fixed window | `INCR`+`EXPIRE` on a clock-aligned bucket. One command, cheap, but allows a 2× burst straddling a window boundary |
| Sliding window log | Sorted set of request timestamps, trimmed and counted each request. Exact, no boundary burst, costs more commands and memory |
| Token bucket | Fixed capacity refilling over time; allows a deliberate burst up to capacity. Needs a Lua script to stay atomic |
| Per-IP key | Used for public endpoints — no user identity exists yet, so it's the only option, enforced in middleware before routing |
| Per-user key | Used behind `get_current_user` — fairness between users, avoids punishing a shared NAT/office IP. Can't live in middleware; must run as a dependency after auth resolves |
| Per-email key (login only) | Catches credential stuffing spread across many IPs at one account. Must not create a response that differs by whether the email is registered — same trap as `user-enumeration-basics.md` |
| `X-Forwarded-For` | Header a proxy uses to pass along the real client IP. Most proxies *append* to it rather than replace it — an attacker can pre-seed it with a fake IP, which "came through the known proxy" alone won't catch |
| Fail open vs. fail closed | What a rate limiter does when Redis is unreachable — let traffic through unprotected, or reject it. A deliberate choice, not a default |
| `Retry-After` | Required header on a `429` — seconds until the caller can reasonably retry |
| `X-RateLimit-*` | Optional limit/remaining/reset headers on every response. Withheld on login specifically — they'd hand an attacker exact telemetry on how close they are to the threshold |
| `(limit, window_seconds)` in `Settings` | Every rate limit is this pair, one per endpoint group — never hardcoded, never loosened per-environment (same `rating_smoothing_m` warning: staging must exercise the same limits production does) |
