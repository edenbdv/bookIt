# From modular monolith to microservices — the theory, and BookIt's plan

The guide for Epic 11 (BOOK-13). Part 1 is general theory: what a microservice split
actually changes and what it costs. Part 2 applies it to BookIt, ticket by ticket — what
each one does, how, and why in that order. The concrete target event flow (diagram,
open design points) lives in `epic-11-flow.md`; read this page first. What was
actually decided along the way, and why: `microservices-decisions.md`.

---

# Part 1 — Theory

## 0. The mental model, if you remember nothing else

```
 MODULAR MONOLITH (BookIt today)            MICROSERVICES (Epic 11's target)

 ┌───────────── one process ─────────────┐   ┌─────────┐  event   ┌─────────┐
 │ booking ──fn call──► payment          │   │ Booking │ ───────► │ Payment │
 │    │                    │             │   │ own DB  │ ◄─────── │ own DB  │
 │    └──fn call──► notification         │   └─────────┘  event   └─────────┘
 │                                       │        │ event
 │        one DB, one transaction        │        ▼
 └───────────────────────────────────────┘   ┌──────────────┐
                                             │ Notification │
                                             │   own DB     │
                                             └──────────────┘
```

In a monolith, modules talk through **function calls** and share **one database**, so
one transaction can cover all of them: either everything commits or nothing does.

In microservices, each service is a **separate program with its own database**. They
talk over the **network** (HTTP calls or events). Nothing can cover two services in one
transaction anymore.

**That last sentence is the whole epic.** Almost every hard part below — sagas,
outboxes, idempotency, eventual consistency — exists because a function call became a
network message and one transaction became several.

## 1. What a monolith is, and why BookIt is a *modular* one

A **monolith** is one deployable application: one codebase, one process (or several
copies of the same one), usually one database.

A **modular monolith** is a monolith whose code is split into modules with clear
boundaries. BookIt is one: `app/auth`, `app/reservation`, `app/payment`,
`app/notification`, each with its own `models.py`, `service.py`, `router.py`. That
matters for Epic 11, because a split follows existing seams — a well-modularized
monolith is the best possible starting point.

Monoliths are not "bad". For one developer or a small team they're usually the right
choice: simple to run, simple to debug, and transactions just work. Most companies that
run microservices started as a monolith and split later, when they had a reason to.

## 2. Why split at all — and what it costs

**What you gain:**

| Gain | Meaning |
|---|---|
| Independent deploys | Change Payment without redeploying Booking |
| Independent scaling | Run 5 copies of Booking during peak season, 1 of Notification |
| Fault isolation | Notification crashing doesn't take bookings down with it |
| Team ownership | One team owns one service end to end, including its data |
| Technology freedom | A service can use a different language or database if it needs to |

**What you pay:**

| Cost | Meaning |
|---|---|
| The network can fail | A call that was a function call can now time out, fail halfway, or arrive twice |
| No shared transactions | "Charge and confirm" can no longer be one atomic step |
| Eventual consistency | For a moment, services disagree (paid, but not yet confirmed) |
| Harder debugging | One user action now spans several processes and logs |
| More infrastructure | More containers, databases, configs, deploys, monitoring |
| Duplicated data | Services keep copies of data they don't own |

**The honest rule:** split when the gains solve a real problem you have. BookIt doesn't
have scaling or team problems — the roadmap's reason is *learning*: "pay the real cost
of distribution deliberately, once, with full understanding". That's a valid reason
for this project, and it's why every cost above gets handled explicitly in Part 2.

## 3. The core concepts

### 3.1 Service boundaries — where to cut

Cut along **business capabilities**, not technical layers. "Payments" is a good
service; "the database layer" or "all the routers" is not.

A good boundary passes three tests:
1. **It owns its data.** Payment owns payments; no one else writes them.
2. **It's cohesive.** Things that change together live together.
3. **Few chatty calls cross it.** If two "services" must call each other on every
   request, they're probably one service.

### 3.2 Database per service

Each service gets its **own database** that only it reads and writes. Others get the
data by asking the service (API) or by listening to its events.

**Why not keep one shared database?** Because then the services aren't really
independent: changing a table breaks someone else's code, and nothing stops service A
from quietly writing service B's rows. A shared database is the classic sign of a
**distributed monolith** — all the costs of microservices, none of the gains.

**What you lose with separate databases:**
- **No JOINs across services.** Booking can't `JOIN payments`.
- **No foreign keys across services.** `payments.reservation_id` becomes a plain UUID;
  nothing enforces that the reservation exists.
- **No transaction across services.** The big one — see §3.5.

### 3.3 Communication: synchronous vs. asynchronous

**Synchronous (request/response — REST, gRPC):** service A calls B and *waits* for the
answer.
- ✅ Simple; you get an answer right away.
- ❌ **Temporal coupling:** if B is down or slow, A is down or slow too. Chains
  (A → B → C) multiply the risk.
- **Use when** the caller genuinely needs the answer now — usually because a user is
  waiting for it.

**Asynchronous (events through a broker — Kafka here):** A publishes "this happened"
and moves on; B reacts whenever it's ready.
- ✅ A doesn't care if B is down right now — the event waits in Kafka.
- ✅ New consumers can be added without touching A.
- ❌ No immediate answer; the system is *eventually* consistent (§3.7).
- **Use when** the reaction doesn't have to happen inside the user's request.

Rule of thumb: **sync for queries the user is waiting on, async for reactions.**

### 3.4 Two kinds of events

- **Event notification:** "reservation 123 was confirmed" — just an id. The consumer
  must call back to get details, which re-creates a sync dependency.
- **Event-carried state transfer:** "reservation 123 was confirmed, guest
  dana@example.com, property Cabin, Sep 1–5". The consumer has everything it needs and
  never calls back.

The second keeps services more independent, at the cost of bigger events and data
copies. BOOK-64 uses it so Notification never needs Booking's tables.

### 3.5 Distributed transactions → the saga pattern

In the monolith, "charge the card, then confirm the reservation" could share a
database. Across services it can't. Two options exist:

- **Two-phase commit (2PC):** a coordinator makes every database "prepare", then
  "commit" together. Rarely used between services — slow, fragile, and it blocks
  everyone if the coordinator dies. Not used here.
- **Saga:** split the business process into **local transactions**, one per service,
  connected by events. If a later step fails, run **compensating actions** that undo the
  earlier steps in business terms.

```
 1. Payment: charge the card        (local transaction in Payment's DB)
 2. Booking: confirm the reservation (local transaction in Booking's DB)
    └─ if 2 fails → compensate 1: refund the card
```

A compensation isn't a rollback — the charge really happened; you undo its *effect*
(refund). BookIt already has a saga (BOOK-54, `docs/payment/saga-basics.md`) — today
it runs in one process; Epic 11 makes it span services.

Two styles:
- **Orchestration:** one component tells each step what to do (BookIt today:
  `payment/saga.py`).
- **Choreography:** no central coordinator; each service reacts to the previous one's
  events. Epic 11 moves toward this: Booking reacts to `PaymentSucceeded`, Payment
  reacts to `BookingCancelled`.

### 3.6 Reliability: outbox, inbox, idempotency

Messages between services are **at-least-once**: an event can be delivered twice, and
publishing can't share a transaction with the database. Three patterns handle that —
all already in BookIt from Epic 7:

- **Outbox** (sender side, `docs/jobs/outbox-basics.md`): write the event into an
  `outbox_events` table **in the same transaction** as the state change; a relay
  publishes it later. The event can't be lost and can't be sent for a change that
  rolled back.
- **Inbox** (receiver side, `docs/jobs/inbox-basics.md`): record each processed event's
  id, and skip ones already seen.
- **Idempotency**: handling the same message twice must have the same effect as once.
  For emails a duplicate is annoying; **for refunds it's money** — so every new consumer
  in Epic 11 gets an Inbox.

### 3.7 Eventual consistency — what users actually see

With events, there's a short window where services disagree:

```
 t=0   Payment: charge SUCCEEDED, PaymentSucceeded in its outbox
 t=1s  relay publishes it to Kafka
 t=2s  Booking consumes it → reservation CONFIRMED
```

Between t=0 and t=2s the money is taken but the reservation still says PENDING. That's
normal and fine — as long as **the system always converges** to a correct state, and
the UI is honest about it ("Payment received, confirming your booking…").

### 3.8 Service-to-service authentication

Inside a monolith, a function call needs no auth. Over the network, Booking's internal
endpoints must know the caller is really Payment, not anyone on the network. Common
answers: **service tokens** (a signed JWT that identifies a service, not a user) or
**mTLS** (both sides present certificates). Plus a hard rule: internal endpoints are
never reachable from the public internet.

### 3.9 API gateway / BFF

After a split, the frontend would have to know several addresses. A **gateway** is one
public entry point that routes each request to the right service (`/payments/*` →
Payment, the rest → Booking), and is the natural place for CORS and for hiding
internal routes. A **BFF** (backend-for-frontend) is a gateway that also reshapes or
combines responses for one specific frontend. In depth: `api-gateway-basics.md`.

### 3.10 The strangler fig — migrate step by step

Named after a vine that grows around a tree until it replaces it. Instead of a big-bang
rewrite, **pull one piece out of the monolith at a time**, and after every step the
whole system still runs. The monolith shrinks gradually; you can stop at any stable
point.

The key skill is knowing **which steps are stable on their own** and which must land
together — Part 2 marks this for every ticket.

## 4. Anti-patterns to avoid

| Anti-pattern | What it looks like | Why it's bad |
|---|---|---|
| Distributed monolith | Services that share a DB, or must deploy together | All the costs, none of the gains |
| Chatty sync calls | Service A calls B five times per request | Slow, and A goes down with B |
| Sync chains | A → B → C → D for one user request | Latency adds up; any one failure fails all |
| Big-bang rewrite | Split everything at once | Nothing works until everything works |
| "Just retry it" without idempotency | Retrying a charge on timeout | Double charges |
| Ignoring the in-between state | Assuming paid ⇒ confirmed instantly | Bugs in the eventual-consistency window |

---

# Part 2 — BookIt's plan

## 5. Where BookIt starts

**Already in place (what makes this epic feasible):**
- Clear modules (`auth`, `reservation`, `payment`, `notification`, …) — the seams to
  cut along.
- **Kafka + outbox + inbox** (Epic 7): reliable events already exist for
  `BookingConfirmed` / `BookingCancelled`.
- **Notification already consumes via Kafka** in its own process (BOOK-51).
- **Idempotency** on reservations and payments (BOOK-36, BOOK-53).
- **The payment code already treats the provider as external:** it commits before and
  after the provider call (BOOK-54/55) — the same discipline a network boundary needs.
- **Integration and contract tests** (BOOK-61/62) and **CI** (BOOK-63) to prove the split
  didn't break anything.

**What's still monolith-shaped (what Epic 11 must change):**
- One shared database; Notification and Payment read Booking's tables directly.
- The saga calls `confirm_reservation()` / `cancel_reservation_system()` as functions.
- Refunds on cancellation are direct calls (`payment/cancellation.py`).
- Foreign keys between `payments` and `reservations`.
- Several flows share one transaction / one `UnitOfWork`.

## 6. The order, and what must ship together

```
 Step 1          Step 2 (one piece)                    Step 3            Step 4
 BOOK-64   ──►   BOOK-65 + BOOK-66 + core of BOOK-68  ──► BOOK-67  ──►  rest of BOOK-68
 Notification    Payment service, its auth,               Gateway        Saga hardening
 (stable alone)  saga over events (not stable apart)     (on top)       (on top)
```

Every arrow is a point where the app fully works and you could stop.

## 7. Ticket by ticket

### BOOK-64 — Notification becomes its own service

**What:** move the Notification module out into its own deployable service with its
own database.

**Why first:** it's the smallest, lowest-risk split — it already talks only through
Kafka for two of its three emails, there's no money involved, and the worst failure is
a missing email. It forces every step of a real extraction (payload design, own DB, no
shared imports) at low stakes, so BOOK-65 starts with that practice done.

**How:**
1. **Stop reading Booking's tables.** Today each email function loads the reservation,
   guest and property through `uow.reservations` / `uow.users` / `uow.properties`.
   Switch to **event-carried state transfer** (§3.4): Booking puts guest email,
   property title and dates into the event payload; Notification uses only that.
2. **The "reservation received" email** is sent through a Celery task, not Kafka
   (`worker-decisions.md` §9). Decide: keep Celery but pass all needed data in the task
   arguments, or move it to a Kafka `BookingCreated` event.
3. **Own database** for its Inbox table (`processed_notification_events`).
4. **Own deployable:** its own entrypoint and image, importing nothing from Booking.

**Theory it exercises:** database per service (§3.2), event-carried state (§3.4),
boundaries (§3.1).

**What can go wrong:** an event missing a field the email needs → email can't be
built. Fix by treating the event payload as a **contract** (versioned, tested).

**Stable alone?** ✅ Yes.

### BOOK-65 — Payment becomes its own service

**What:** move Payment — charges, refunds, webhook, reconciliation — into its own
service with its own database.

**Why it's the core of the epic:** this is where money crosses a service boundary, so
it's where every cost from §2 shows up at once.

**How:**
1. **Own database** for `payments` and its idempotency keys. `payments.reservation_id`
   loses its foreign key — it becomes a plain UUID (§3.2).
2. **Read the reservation synchronously.** `POST /payments` needs the guest, status and
   amount before charging, and a user is waiting → a **sync call** to Booking (§3.3),
   authenticated per BOOK-66.
3. **Publish the outcome as events.** Payment gets its own outbox and a new
   `payment-events` topic, keyed by `reservation_id`: `PaymentSucceeded` /
   `PaymentFailed`, written in the same transaction as the payment's status (§3.6).
4. **Refunds become reactions.** Instead of `cancellation.py` calling Payment directly,
   Payment consumes `BookingCancelled` and refunds when there's a succeeded charge. The
   refund amount (policy lives in Booking) travels in the event.

**Why it can't ship alone:** the moment Payment is a separate program, the saga's
`confirm_reservation()` call no longer exists for it — so a successful payment would
confirm nothing. It needs BOOK-68's core (Booking consuming `payment-events`) and
BOOK-66's auth (for the sync call) in the same release.

**Theory it exercises:** database per service, sync vs. async, outbox, saga.

### BOOK-66 — Service-to-service auth

**What:** make sure Booking's internal endpoints only accept calls from trusted
services.

**Why now:** BOOK-65 introduces the first service-to-service call. Without auth, anyone
who can reach the network could read any reservation.

**How (decision to make):**
- **Service tokens:** Payment presents a signed JWT identifying it as a service (a
  `service` claim, not a user). Reuses Epic 3's JWT code. Simplest.
- **mTLS:** both sides present certificates. Stronger, but certificates must be issued
  and rotated.

Either way: internal endpoints under something like `/internal/…`, a guest token must
not work there, and a service token must not work on guest routes.

**Theory:** §3.8. **Stable alone?** Ships with BOOK-65.

### BOOK-68 (core) — the saga over the network

**What:** the steps that used to be function calls become events between services
(choreography, §3.5).

**How:**
1. Booking consumes `payment-events` in its own consumer group:
   `PaymentSucceeded` → confirm, `PaymentFailed` → cancel. Confirming/cancelling still
   publishes `BookingConfirmed` / `BookingCancelled` through Booking's existing outbox,
   so Notification and Payment hear about it.
2. **An Inbox in every new consumer** (§3.6) — relays are at-least-once, and a
   duplicate refund is real money.

**Stable alone?** Its core ships with BOOK-65.

### BOOK-67 — API gateway / BFF

**What:** one public entry point in front of the services (§3.9).

**Why after BOOK-65:** until there are two services there's nothing to route between.

**How:** route by path (`/payments/*` → Payment, the rest → Booking); keep `/internal/*`
unreachable from outside; one place for CORS. Start with a plain reverse proxy
(nginx/Traefik in docker-compose); only build a FastAPI BFF if the frontend needs
combined responses.

**Stable alone?** ✅ It's an improvement on top. (Without it, the frontend can
temporarily use two base URLs.)

### BOOK-68 (hardening) — the failure cases a network adds

**What:** handle what can now go wrong between services. Each case needs a defined
outcome where the money and the reservation end up consistent:

| Case | What must happen |
|---|---|
| Charged, but confirming fails (e.g. the hold expired meanwhile) | Booking publishes a failure event → Payment refunds. Replaces today's in-process `_refund_and_release` |
| Payment read the reservation, then it changed | Booking re-checks it's still PENDING when `PaymentSucceeded` arrives |
| The expiry job cancels a hold while a payment is in flight | A late `PaymentSucceeded` for a cancelled reservation must end in a refund, never a silent charge |
| Events delayed, duplicated or out of order | Inbox dedup; per-key ordering holds only *within* one topic, so never rely on order *across* `payment-events` and `booking-events` |

**Done when:** every money path ends in one of two states — *confirmed and charged*, or
*cancelled and not charged (or refunded)* — including under redelivery, proven by
integration tests in the BOOK-61 style.

## 8. How you'll know each step worked

- **BOOK-62's contract test** (no double booking, through the public API) must pass
  unchanged after every step — that's what "contract" bought us.
- BOOK-61's saga and webhook tests get adapted from "call a function" to "publish an
  event, wait for the reaction".
- CI (BOOK-63) runs it all on every PR.

---

## Cheat sheet

| Concept | One line |
|---|---|
| Monolith | One deployable, one DB, function calls, shared transactions |
| Microservice | Separate program, own DB, talks over the network |
| Database per service | Only the owner touches its data — no cross-service JOINs, FKs or transactions |
| Sync (REST/gRPC) | Caller waits; use when a user needs the answer now |
| Async (events) | Fire and react later; use for reactions |
| Event-carried state | Put what consumers need in the event, so they never call back |
| Saga | A multi-service process as local transactions + compensations |
| Orchestration / choreography | One coordinator vs. services reacting to each other's events |
| Outbox / Inbox | Reliable sending / safe duplicate receiving |
| Eventual consistency | Services briefly disagree, then converge |
| Service auth | Prove the caller is a trusted service (service token or mTLS) |
| Gateway / BFF | One public entry point routing to services |
| Strangler fig | Extract one piece at a time; the app works after every step |
| Distributed monolith | Split code, shared DB or lockstep deploys — the thing to avoid |
| BookIt's order | 64 → (65 + 66 + core 68) → 67 → 68 hardening |
