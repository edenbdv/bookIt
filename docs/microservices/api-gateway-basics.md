# API gateway basics (BOOK-67)

**Status: built (BOOK-67).** 67a: routing, CORS, `/internal` blocked, only the gateway
published. 67b: JWT checked at the edge (forward auth), `X-User-Id`, per-user limit on
payments. All Traefik, configured in `gateway/dynamic.yml`. The short version is in
`microservices-basics.md` §3.9; this page goes deeper. The items
BOOK-65/66 handed over are in `microservices-decisions.md` §19.

## 1. The problem: the browser now talks to two services

After BOOK-65 the frontend knows **two addresses** — Booking on `:8000`, Payment on
`:8001` — and both have to:

- allow the frontend's origin (**CORS**), each on its own;
- **verify the guest's token** (Payment does it itself today, holding `SECRET_KEY`);
- be **reachable from outside** — including Booking's `/internal/*`, which should only
  ever be called by Payment (BOOK-66 protects it with a token, but it's still exposed).

Every new service would add another address to the frontend and another copy of these
concerns. That's the problem a gateway solves.

## 2. What an API gateway is

**One public entry point** in front of all the services. The browser only knows the
gateway; the gateway forwards each request to the right service, inside the network.

```
                        ┌──────────────── private network ───────────────┐
browser ──► GATEWAY ────┼─► /payments/*  → Payment  (payment-api:8001)    │
          (one address) │                                                 │
                        └─► everything else → Booking (app:8000)          │
                            /internal/*  ✗ blocked at the gateway         │
                        └─────────────────────────────────────────────────┘
```

The services themselves are no longer published to the outside at all — only the
gateway is.

## 3. What belongs in a gateway — and what doesn't

**In the gateway: cross-cutting, the same for every request.**

| Job | What it means for BookIt |
|---|---|
| **Routing** by path | `/payments/*` → Payment, the rest → Booking |
| **Hiding internals** | `/internal/*` never reachable from outside |
| **CORS** | one origin rule, in one place |
| **Authentication** (who are you?) | verify the guest's JWT once, pass the user id on |
| **Rate limiting** | per user / per IP, before any service is hit |
| TLS termination, request size limits, logging, request ids | the usual edge chores |

**Not in the gateway: business logic.** "May this guest pay for this reservation?" is
*authorization* about domain data — it stays in Payment (it calls Booking for that,
§17). A gateway that starts knowing about reservations and refunds becomes a new
monolith in front of the microservices.

Rule of thumb: the gateway answers **"who is this, and where does it go?"** — the
services answer **"is it allowed, and what happens?"**

### Hiding `/internal` (67a)

`/internal/*` is for **service-to-service** calls only — Payment asking Booking about a
reservation (BOOK-66). That call never goes through the gateway: Payment calls Booking
directly inside the Docker network (`BOOKING_BASE_URL=http://app:8000`).

So the gateway simply has **no route** for it:

| Router | Rule | Matches `/internal/...`? |
|---|---|---|
| `payment` | `` PathPrefix(`/payments`) `` | no |
| `booking` | `` PathPrefix(`/`) && !PathPrefix(`/internal`) `` | no — excluded on purpose |

No match → Traefik answers **404** itself; the request never reaches Booking.

Two layers:

1. **Gateway hides it** — from outside, the route doesn't exist (404).
2. **Service token still guards it** (BOOK-66) — inside the network, `/internal` needs
   Payment's token (401 otherwise).

Layer 1 only holds once Booking's port isn't published (step 5) — before that,
`localhost:8000/internal/...` went around the gateway.

## 4. Authentication at the edge

Today (BOOK-65, §19) Payment verifies the guest's JWT itself. With a gateway:

```
browser ──Authorization: Bearer <JWT>──► gateway
                                           │ verify signature + exp + type
                                           │ (fail → 401, request never reaches a service)
                                           ▼
                                         Payment ◄── X-User-Id: <sub>   (no JWT, no secret)
```

- **One place holds the user-login secret** (or, with RS256, the public key) —
  Payment stops holding `SECRET_KEY`.
- **Services trust a header** — `X-User-Id` — instead of a token.

**The catch: that header must be impossible to fake.** If a request could reach Payment
*without* going through the gateway, anyone could send `X-User-Id: <someone else>`.
So it only works when:

1. the services are **not reachable from outside** (only the gateway is published), and
2. the gateway **strips any incoming `X-User-Id`** from the client before setting its own.

This is the gateway's **trust boundary**: outside it, nothing is trusted; inside it, the
gateway's headers are. (In stricter setups the gateway passes on a signed token instead
of a plain header, so services can still verify it — "zero trust".)

## 5. Rate limiting at the edge

Booking limits per user and per IP inside its own app (`enforce_user_rate_limit`, the
`RateLimitMiddleware`); Payment dropped its limit in BOOK-65 (§19). At the gateway:

- every service is protected the same way, by one rule set;
- abusive traffic is rejected **before** it costs any service a database query;
- it needs a shared counter store (Redis) so several gateway instances agree.

Per-**user** limits need the user id — so they come *after* authentication in the
gateway; per-**IP** limits can come first.

### BookIt's limit (67b)

`payment-rate-limit` in `gateway/dynamic.yml`, on guest `/payments` routes only: per
`X-User-Id`, a **burst of 5, then 10/min** — past that, **429**. Order matters:

```
cors → strip-user-id → auth → payment-rate-limit → Payment
```

- after `auth`, so the key is the **real** user id — a client faking `X-User-Id` is still
  counted as itself (`strip-user-id` + forward auth replace it);
- no token → `auth` answers 401 first, so anonymous requests never reach the counter.

Booking keeps its own limits (above) — the gateway's covers Payment, which had none.

**Where the counters live: in memory**, inside the gateway.

| | In memory (Traefik v3.1) ✅ | Redis |
|---|---|---|
| Setup | Config only | Newer Traefik (3.4+) can use Redis — a version bump + our `redis` |
| One gateway | Fine | Fine |
| Several gateways | Each counts on its own (limit × N) | Shared — they agree |
| Gateway restart | Counters reset | Counters kept |

One gateway today, so in memory is enough. Switch to Redis once there's more than one
gateway instance. (A third way — counting in `/auth/verify` with Booking's
`check_fixed_window` — would use our Redis now, but puts the limit back inside Booking.)

## 6. Gateway vs. BFF vs. the other boxes people mention

| Thing | What it does | Relation to BookIt |
|---|---|---|
| **API gateway** | Generic edge: route, authenticate, limit — knows URLs, not the domain | What BOOK-67 needs |
| **BFF** (backend-for-frontend) | A gateway made *for one frontend*: may also **combine** calls (e.g. "reservation + its payment" in one response) or reshape data for that UI | Only if the React app starts needing combined views |
| **Reverse proxy** (Nginx, Traefik) | Forwards requests by host/path — the core of most gateways | The simplest way to build BOOK-67's routing |
| **Load balancer** | Spreads traffic over several copies of *one* service | Not needed — one copy of each service |
| **Service mesh** (Istio, Linkerd) | Handles service-*to*-service traffic (mTLS, retries) *inside* the network | Different layer — BOOK-66's service tokens play that role here |

A gateway faces the **outside** (north–south traffic); a mesh handles traffic
**between** services (east–west).

## 7. Building one: ready-made proxy vs. a small app

| | Ready-made proxy (Nginx / Traefik / Envoy / Kong) | Small FastAPI app (a BFF-style gateway) |
|---|---|---|
| Routing, CORS, blocking paths | Configuration only | A few routes forwarding with httpx |
| JWT verification | Plugins, or **forward auth**: the proxy asks a tiny auth endpoint "is this token OK?" before routing | Plain Python — reuses the JWT code BookIt already has |
| Rate limiting | Built in (Nginx `limit_req`, Traefik middleware), mostly per IP | `check_fixed_window` + Redis, as Booking does today, per user |
| Performance | Very fast, battle-tested | Fine at BookIt's scale; one more Python hop |
| What it teaches | How real infra is configured | Gateway logic, visible in code |

**Forward auth** is the common middle path: Traefik/Nginx do the routing, and for every
request call a small endpoint (`/auth/verify`) that checks the JWT and returns the user
id in a header — the proxy copies it onto the forwarded request.

## 8. Costs of a gateway

- **One more hop** on every request (small, but real).
- **A single point of failure** — if the gateway is down, everything is. In production
  it runs as several copies behind a load balancer.
- **A bottleneck for changes** if teams must edit one shared gateway config — mitigated
  by keeping it generic (routes, not logic).
- **Temptation to put logic in it** (§3) — the main way gateways go wrong.

## 9. BookIt's plan for BOOK-67

Split in two, so each half is done end to end:

**67a — the core (routing):**

1. ✅ A `gateway` service in docker-compose, the **only** one with a public port for the API.
2. ✅ `/payments/*` → `payment-api:8001`, everything else → `app:8000`.
3. ✅ `/internal/*` → **404 at the gateway**.
4. ✅ CORS configured once; the frontend back to **one** base URL
   (`VITE_PAYMENT_API_BASE_URL` goes away).
5. ✅ Booking's and Payment's ports no longer published to the host.

**67b — auth and limits at the edge** (the items handed over, §19):

1. ✅ Verify the guest's JWT at the gateway; strip any incoming `X-User-Id`, set it from the
   token.
2. ✅ Payment reads `X-User-Id` instead of verifying the token → remove `get_guest_id`'s
   JWT check and `SECRET_KEY` from Payment.
3. ✅ Per-user rate limiting at the gateway.

**Decision (67a):** a ready-made proxy — **Traefik** (§7), no gateway code to write.

**How it's configured: one file, not Docker labels.** Traefik can read routes from labels
on each container (the *docker provider*) or from a file (the *file provider*). BookIt
uses the file — `gateway/dynamic.yml`:

| | Labels on each service | One file ✅ |
|---|---|---|
| Where routes live | Scattered over `app`, `payment-api`, … | All in one place |
| Readability | Long one-line strings | Plain YAML |
| Docker socket | Needed — Traefik watches Docker (a lot of power) | Not needed |
| New service | Add labels to it, nothing else | Edit the gateway file |

Labels pay off when containers come and go dynamically; with two fixed services the file
is simpler. After editing it: `docker compose restart gateway` (Traefik can watch the file, but
change events don't reach the container on Docker Desktop for Windows).

### Who checks the JWT? (67b decision)

| | Who checks | Pros | Cons |
|---|---|---|---|
| **A. Forward auth → Booking** ✅ | Booking's `/auth/verify` | Fastest; reuses `get_current_user`; also rejects deleted users | Booking is in every payment request; the gateway *enforces* but doesn't *check* |
| **B. Traefik JWT plugin** | The gateway itself | Really the gateway's job, no extra hop | Plugin downloaded at Traefik startup, more setup |
| **C. Small `auth` service** next to the gateway | A tiny container that only verifies | Clean split: Booking issues, `auth` checks, Payment trusts the header | One more container; needs `SECRET_KEY` too (or only the public key, with RS256) |

**Chosen: A** — the easiest and fastest. What matters still holds: no valid token → stopped
at the edge, and Payment no longer holds `SECRET_KEY`. **C** is the cleaner design if auth
should really belong to the edge later.

In all three, Booking still checks tokens on **its own** routes — making Booking trust
`X-User-Id` too is a bigger change (public routes, user lookups).

## 10. Is the design right? Known weak spots

**What holds:** one way in (only the gateway is published, `/internal` hidden, CORS once);
the trust boundary (`X-User-Id` stripped, set only by forward auth, services unreachable
from outside); business rules stay in the services. Option A adds no new coupling —
Payment already calls Booking on every payment (BOOK-66's reservation read).

**Where it's weaker — deliberate, for now:**

| Weak spot | Why it's OK now | When to change it |
|---|---|---|
| **Rate limits in two places, two ways** (Booking: in-app, Redis; Payment: gateway, in memory) | Some of Booking's can't move anyway — login's per-email limit needs the request body | More services → one per-IP limit at the gateway, domain limits stay in the services |
| **Two auth models** (Booking checks JWTs itself; Payment trusts the header) | Booking has public routes and user lookups (§9, option C) | If auth should belong to the edge → a separate `auth` service |
| **The header is trusted only because of the network** | Compose's network is private, ports are closed | Production → a signed header or mTLS ("zero trust", §4) |
| **Gateway counters in memory** | One gateway | Several → Redis (§5) |

None of these is a bug: each has a reason, and a point at which to change it.

## Cheat sheet

| Question | Answer |
|---|---|
| What is it? | One public entry point that routes to the services |
| What goes in it? | Routing, hiding internals, CORS, authentication, rate limiting |
| What stays out? | Business rules / domain authorization — those stay in the services |
| How do services know the user? | The gateway verifies the JWT and forwards `X-User-Id` |
| Why can't a client fake `X-User-Id`? | Services aren't reachable from outside, and the gateway strips the incoming header |
| Gateway vs. BFF? | BFF = a gateway for one frontend, may combine/reshape responses |
| Gateway vs. service mesh? | Gateway: outside → in (north–south). Mesh: service ↔ service (east–west) |
| Biggest cost? | A single point of failure + one more hop |
