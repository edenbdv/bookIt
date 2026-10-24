# What BookIt chose for its microservice split, and why

The Epic 11 counterpart to `microservices-basics.md`: that page explains the general
concepts and the overall plan; this one records what this project actually decided,
ticket by ticket, and when each decision should be revisited. Same format as
`docs/jobs/worker-decisions.md` and `docs/testing/testing-decisions.md`.

---

# Epic-wide

## 0. What gets split out, and what stays together in Booking

**Chosen:** only two services leave the monolith — **Notification** (BOOK-64) and
**Payment** (BOOK-65). Everything else stays together in what this doc calls
**Booking**: really the remaining monolith, one service with one database.

```
 Booking (remaining monolith, one DB)        Notification       Payment
   ├── auth / users                          (own DB)           (own DB)
   ├── properties
   ├── reservations
   └── reviews
```

**Honestly, none of these splits is *required* in BookIt** — the roadmap's reason for
Epic 11 is learning (`microservices-basics.md` §2). But if parts are going to be split,
these two are the natural candidates, because each passes **both** tests a split needs:

1. **It has a reason of its own** to be separate — isolation, scale, security, or an
   external system the rest of the app doesn't share.
2. **It has a clean boundary** — few calls crossing it (`microservices-basics.md` §3.1).

**Notification — why it's worth splitting:**
- **Fault isolation.** A slow or down mail server must never affect bookings. As its own
  service, an SMTP problem stays inside it.
- **It only receives.** Events in, emails out; nobody waits on it, nobody asks it
  anything. The split costs almost nothing.
- **A different load pattern.** Email sending runs in the background, sometimes in
  bursts, and can scale on its own.

**Payment — why it's worth splitting:**
- **An external system with its own failure modes** — the provider, timeouts, the
  webhook, reconciliation. A world of its own.
- **Security — the real-world reason.** Payment code is held to strict standards
  (PCI-DSS). Keeping it in a small, separate service means less code and fewer people
  ever near sensitive data.
- **Its own data** — charges and refunds, clearly owned by one service.

**Why users / properties / reservations / reviews stay together** — they fail test 1
(no separate load, no external system of their own, no special security requirement)
*and* test 2, because they work together on almost every request:
- creating a reservation needs the property (price, `max_guests`, host) and the guest;
- cancelling needs the property's cancellation policy;
- a review needs the reservation and the property;
- listing properties needs review stats.

Splitting them would turn each of those into synchronous calls between services —
`microservices-basics.md` §4's "chatty services" — paying every cost of distribution
for no gain. Inside Booking they keep sharing one DB, one transaction, and plain JOINs.

**Consequence for BOOK-64:** when Booking writes an event, it's free to read `users`
and `properties` to fill the payload (§5) — same service, same DB.

**Revisit when:** a part of Booking develops its own reason to split — e.g. an
"identity" service for users/login if other systems ever need to authenticate against
it. Not on the roadmap.

---

# BOOK-64 — Notification as its own service

## 1. The starting point: six ties to the monolith

Notification already consumes Kafka in its own process (BOOK-51), but it's still a
module of the monolith wearing a separate container. Six things tie it to Booking:

| # | Coupling | Where |
|---|---|---|
| 1 | All three email functions read Booking's tables (reservation, guest, property) | `notification/service.py` — `uow.reservations` / `uow.users` / `uow.properties` |
| 2 | The event is too thin to send an email from: just `event_id`, `event_type`, `reservation_id`, `occurred_at` | `reservation/service.py`, `_emit_outbox_event` |
| 3 | A foreign key *between services*: the Inbox table points at Booking's outbox table | `notification/models.py` — `ForeignKey("outbox_events.id")` |
| 4 | Shared code: the monolith's `UnitOfWork`, `get_engine`, `get_settings`, `OutboxEventType` | `notification/consumer.py`, `app/notification_consumer.py` |
| 5 | The "reservation received" email runs in the monolith's Celery worker, triggered by `.delay()` | `app/worker.py`, `reservation/service.py` |
| 6 | Same image (`build: .`) and the same `bookit` database | `docker-compose.yml`, `notification-consumer` service |

## 2. The plan: six small steps, the app works after each

**Status: done.** All six ties are gone — Notification runs from `services/notification/`,
its own image and its own `notifications` database, and imports nothing from Booking
(§8, §9).

1. **Lock the current behavior with tests** — the Inbox dedup and the email contents —
   before changing anything (BOOK-60 item 3).
2. **Enrich the event** with everything the emails need, and switch the handlers to
   read the payload instead of the DB. Still inside the monolith. Removes ties 1–2.
3. **Move "reservation received" to Kafka** as `BookingCreated` (§4). Removes tie 5.
   - **3d, added along the way:** give the consumer the failure handling the Celery
     task used to have — restart, retry, dead-letter topic (§7).
4. **Own database + migrations** for Notification; drop the cross-service foreign key.
   Removes tie 3.
5. **Extract the code** into its own service with no imports from `app`, its own image
   and compose service. Removes ties 4 and 6.
6. **Tests + this doc** brought up to date.

## 3. Repo structure: a separate service folder in the same repo (monorepo)

**Chosen:** `services/notification/` — its own `pyproject.toml`, `Dockerfile`, config
and migrations, in the same Git repo as the monolith.

| Option | Why not / why |
|---|---|
| **A. `services/notification/` in this repo** ✅ | Real independence: own dependencies, own image. It physically *can't* import from `app` — the structure itself enforces the boundary. Still one repo, so one PR and one CI run for a change that touches both sides of an event |
| B. Stay inside `app/`, promise not to import | Nothing stops an `import app.reservation...` slipping in by accident — the classic start of a distributed monolith (`microservices-basics.md` §4) |
| C. A separate Git repo | Full separation, but every event change becomes two PRs and two CI runs. Too much overhead for one developer |

**Bonus:** BOOK-65 then lands as `services/payment/` right next to it — same layout,
same CI pattern.

**Revisit when:** different people own different services and want separate release
cycles — then a split into repos (option C) starts paying for itself.

## 4. "Reservation received" moves from Celery to Kafka (`BookingCreated`)

**Chosen:** Booking publishes a `BookingCreated` event through its existing outbox;
Notification consumes it from `booking-events` like the other two. All three emails
arrive the same way.

**This revisits an explicit earlier decision.** `worker-decisions.md` §9 kept this email
on a direct Celery `.delay()` "permanently": one action → one email is the textbook
task-queue case, and routing it through Kafka would be forcing every side effect
through an event bus just because one exists. **That reasoning was right for a
monolith.** §9's own "revisit when" was *"a second real consumer of 'reservation
created' gets planned"* — and once Notification is a separate service, the event bus
*is* the interface between services. The circumstances changed, so the decision does.

### Why a Celery task across services is coupling — command vs. event

Both are contracts, so coupling never drops to zero. What differs is **who depends on
whom**, and **how much the sender knows about the receiver**.

**A Celery task is a command** — "run this function for me":

```python
send_reservation_received_email.delay(reservation_id)
```

To send it, Booking must know that Notification exists, that it's responsible for this
email, the task's exact name, and its arguments. **Booking depends on Notification**:
rename the task, add an argument, split it in two, and Booking's code must change — or
it fails silently, with a message on RabbitMQ that no worker recognizes.

**A Kafka event is a fact** — "this happened":

```
BookingCreated { event_id, reservation_id, guest_email, property_title, check_in, ... }
```

Booking says what happened and nothing more. It doesn't know who's listening or that an
email exists. The rule "a new booking means an email" lives inside Notification.
**Notification depends on Booking** (on the event's shape) — the natural direction:
whoever reacts depends on whoever reports.

| Scenario | Celery (command) | Kafka (event) |
|---|---|---|
| Notification renames its function | Booking breaks | Booking unaffected |
| Also send an SMS | Booking adds another call | Notification decides alone |
| An analytics service wants new bookings | Booking adds another call | It subscribes; Booking unchanged |
| Notification is down | Task waits in RabbitMQ | Event waits in Kafka |

**The honest caveat:** with an event, the *payload's shape* becomes the contract —
remove `guest_email` and Notification breaks. That's still coupling, but on data rather
than another service's internals, and pointing the right way.

### Further reasons, specific to BookIt

- **One inbound channel.** Notification listens only to Kafka — no Celery worker and no
  RabbitMQ dependency inside it.
- **Durability.** The current trigger is `uow.on_commit()`, which `worker-decisions.md`
  §9 itself notes is in-memory only: a crash right after commit silently loses the
  email. The outbox writes the event in the same transaction as the reservation.
- **The blocking `.delay()` goes away on this path.** BOOK-62 measured Celery's `.delay()`
  freezing the event loop for ~4s per booking when RabbitMQ is unreachable. An outbox
  row is a plain insert.

**Cost accepted:** one more outbox row per new booking, and an ordering point to
respect — `BookingCreated` goes on the same topic, keyed by `reservation_id`, so it
always precedes that reservation's `BookingConfirmed` / `BookingCancelled`
(`kafka-basics.md` §3).

**Revisit when:** never in the "back to Celery" direction while Notification is a
separate service.

## 5. Events carry the data (event-carried state transfer)

**Chosen:** the event payload includes everything an email needs — guest email and name,
property title / city / country, dates, guest count, total, hold expiry — so
Notification never reads Booking's data (`microservices-basics.md` §3.4).

**Not chosen:** Notification calling Booking's API for the details. It works, but puts
a synchronous dependency back: Booking down ⇒ no emails, even though the event itself
arrived fine.

**Cost accepted:** bigger events, and data copied at event time. That copy is a feature
here, not a bug: an email should describe the booking *as it was* when the event
happened, not whatever the reservation looks like by the time the email is sent.

**The payload is now a contract** between two services: add fields freely; never
remove or rename one without updating the consumer first.

**This reverses BOOK-49's thin payload, on purpose.** BOOK-49 chose a thin event (just
ids) so that consumers re-fetch the *current* state — a delayed or redelivered event
could then never show stale data. That was right while the consumer shared Booking's
DB. It can't survive the split: the whole point of BOOK-64 is that Notification no
longer reads Booking's tables, so it can't re-fetch anything. The trade is the one
above — a snapshot instead of current state — and for an email the snapshot is the
correct one. The code comment above `_emit_outbox_event` (`reservation/service.py`) is
updated to match.

**Implementation consequence: `_emit_outbox_event` becomes `async`.** Filling the
payload means loading the guest and the property, and every DB read in this app is an
`await` (async SQLAlchemy). A plain `def` can't `await` — and lazy-loading
`reservation.guest` doesn't work either: async SQLAlchemy raises `MissingGreenlet`
instead of silently querying. So `_emit_outbox_event` and `cancel_reservation_system`
(its only sync caller) become `async`, and their six call sites gain an `await`. All
six callers were already `async`, so nothing else changes shape.

**Revisit when:** a payload grows large or carries data Booking shouldn't share — then
send only what each consumer needs, or fall back to a lookup for that one field.

## 6. Changing a path without breaking it: expand and contract

**The problem it solves.** In production a change never lands everywhere at once:
- Booking and Notification are **deployed separately**, minutes or hours apart.
- During a deploy, **old and new versions run side by side** for a while.
- Some messages are **already in flight** — Celery tasks waiting in RabbitMQ, events
  waiting in Kafka.

Switch everything in one go and there's a window where one side sends something the
other doesn't understand yet, or an old message arrives at code that no longer exists:
lost emails, or a stuck consumer.

**Chosen: expand and contract** (also called *parallel change*) — three phases:

| Phase | What happens | Moving "reservation received" to Kafka |
|---|---|---|
| **1. Expand** | Add the new path **next to** the old one; both work | Notification learns `booking_created` and builds the email from the payload. The old Celery path keeps sending, through a temporary copy of the old function (`send_reservation_received_email_from_db`) |
| **2. Switch** | Move the sender to the new path | Booking writes `BookingCreated` to the outbox instead of calling `.delay()` (step 3b) |
| **3. Contract** | Delete the old path once nothing uses it | The Celery task and the `_from_db` copy go (step 3b — in a real deployment, only once no old tasks are left in the queue) |

A temporary test pins the two versions to **byte-for-byte identical emails** while both
exist, then goes with the old one.

**The ordering rule inside it: the receiver learns first, then the sender starts.** If
Booking published `BookingCreated` *before* Notification had a handler, the consumer
would hit an unknown type on every one of those events. For an event contract that's
the general rule: deploy the consumer that understands a change before the producer
that emits it. (For *removing* a field it's the reverse: the consumer stops reading it
first, then the producer stops sending it — §5.)

**The safety net under it: unknown event types are skipped, not a crash.** Before
BOOK-64 the dispatcher did `email_handlers[event_type]`. An unknown type raised
`KeyError`, so the message was never committed, Kafka redelivered it forever, and
every message behind it waited — a **poison message** blocking the whole queue. Now an
unknown type is logged as a warning and skipped (`notification/consumer.py`). That's
also the correct behavior long-term: Booking may publish events Notification doesn't
care about (an analytics-only event, say), and "not mine" is a normal answer.
**Trade-off accepted:** a handler forgotten by mistake means a silently missing email —
but with a warning in the log, and every other email still flowing, which beats a
stopped queue.

**Why it's worth it here, not just in production:**
- **Every commit works on its own** — CI stays green and the app runs at each step.
- **Easy to undo** — revert only the switch, and the old path is still there.
- It's the same discipline teams use to change APIs, DB schemas and events without
  downtime.

**Revisit when:** never as a principle. The *contract* phase can be shortened when
there are no deployments to coordinate and no messages in flight — as in tests.

## 7. When handling an event fails: restart, retry, dead-letter (step 3d)

**The problem, found while moving "reservation received".** Any exception while handling
an event — SMTP down, the DB unreachable for the Inbox check, a missing payload field,
a bug — escaped the consumer's `while True` loop and **ended the whole process**.
`docker-compose.yml` had no restart policy, so it stayed down: from that moment *no*
email went out, until someone restarted it by hand. A permanent error (a refused
address) was worse: every restart hit the same message and died again — a crash loop.

This had been true for "confirmed" / "cancelled" since BOOK-51. Moving "received" off
Celery (§4) made it a **regression** for that email: the Celery task had retries with
backoff, and a dead-letter queue for permanent failures (BOOK-47). So the consumer gets
the same protection, in three layers:

| Layer | What it does | Where |
|---|---|---|
| **1. Restart** | If the process dies anyway, Docker brings it back; Kafka redelivers the uncommitted message | `restart: unless-stopped` on `notification-consumer` |
| **2. Retry with backoff** | A *temporary* error is retried in place — 4 retries, waiting 1, 2, 4, 8 s (~15 s) — without crashing | `_with_retry` in `notification/consumer.py` |
| **3. Dead-letter topic** | A message that fails *for good* (permanent error, retries exhausted, not even valid JSON) is published to `booking-events.dlq` and **committed**, so the queue keeps moving | `_process_one` + `make_dead_letter` |

### Temporary vs. permanent — and the bug a test found

Only temporary SMTP errors are retried (`TRANSIENT_ERRORS`: connection errors,
disconnects, `OSError`). A refused address, a refused sender, bad credentials won't fix
themselves (`PERMANENT_ERRORS`) — straight to the DLQ, no retries.

**Every `smtplib` error subclasses `OSError`.** So listing `OSError` as transient also
caught the permanent ones — a refused address was retried anyway. The old Celery policy
had exactly this bug: its comment said refused addresses were excluded, but `OSError` in
`autoretry_for` caught them. Nobody noticed, because nothing tested it. The fix:
`PERMANENT_ERRORS` is checked **first** and re-raised. `test_permanent_error_is_not_retried`
is the test that found it.

### The dead-letter topic

- **Hand-rolled**, because Kafka has no broker-side dead-lettering (BOOK-48 called this
  out). It's just another topic: `<topic>.dlq`.
- **The value is the original message, byte for byte** — so it can be inspected, or
  replayed onto the main topic once the cause is fixed. *Why* it failed travels in
  headers (`error_type`, `error`, `failed_at`), not wrapped around the value.
- **Order: publish to the DLQ, wait for Kafka to confirm, then commit.** If the DLQ
  publish itself fails, that raises *before* the commit: the message stays uncommitted
  and is redelivered after the restart. A message is never silently dropped — the one
  rule none of the layers may break.
- **Not marked processed.** The Inbox row is written only when an email actually went
  out, so replaying a dead-lettered message later still sends it.

### Accepted trade-offs

- **Retries hold the partition.** While one message waits through its ~15 s of retries,
  messages behind it on the same partition wait too. Acceptable for email; it also
  preserves per-reservation order.
- **The Inbox transaction stays open during retries** — one DB connection held for up to
  ~15 s. Fine for a single consumer; worth revisiting if Notification scales out.
- **The DLQ loses the original key** (only the value is republished). Ordering inside
  the DLQ doesn't matter — it's for inspection and replay, not live processing.
- **Nothing reads the DLQ yet.** Inspecting and replaying it is manual (kafka-ui at
  `localhost:8090`). An alert on "DLQ not empty" belongs to Epic 9 (observability).

**Tests:** unit tests for each layer (`services/notification/tests/test_consumer.py`),
plus one against real Kafka
(`test_failing_event_goes_to_dead_letter_and_the_queue_keeps_moving`): a refused
address lands on `.dlq` with its error, and the next guest's email still goes out.

**Revisit when:** the consumer scales to several instances (the open transaction during
retries), or failures get frequent enough that a replay tool or a DLQ alert is worth
building.

## 8. Notification's own database (step 4)

Notification owns exactly one table: `processed_notification_events`, its Inbox (dedup
by `event_id`). Step 4 moved it out of Booking's database, in the same expand-and-contract
shape as §6.

### Where the new database lives: same server, new database

**Chosen:** a `notifications` database on the **same Postgres server** (the `db`
container) as Booking's `bookit` — not a second Postgres container.

| Option | Why not / why |
|---|---|
| **A. New database, same server** ✅ | The separation that matters is the *database*: its own tables, its own migrations, its own connection string, no foreign key or JOIN across. One container less to run and to start in tests |
| B. Its own Postgres container | Closer to production, but another container for no gain the code can see — it's just a different URL, so moving later is a config change |

**Revisit when:** there's a real deployment — there you'd likely separate servers too,
and only `NOTIFICATION_DATABASE_URL` changes.

### Existing Inbox rows: start empty

**Chosen:** the new Inbox started **empty**; the old rows weren't copied.

Safe here: dev data only, and those events' Kafka offsets were already committed, so
none of them comes back to be deduped. The theoretical risk — a message redelivered
exactly at the switch could send one duplicate email — is accepted for a dev stack.
**In a real deployment** you'd copy the rows into the new database *before* the contract
step (expand and contract, §6).

### The cross-service foreign key had to go — and it was hiding a bug

`processed_notification_events.event_id` had a foreign key into Booking's
`outbox_events.id`. It went first (step 4a, migration `1db36abe6069`), for three reasons:

- **A foreign key can't cross databases**, and the two tables were about to live in two.
- **Dedup never needed it.** The `event_id` arrives in the event payload; uniqueness is
  the primary key's job, and that stays. The same value is the key in both services'
  tables — the *value* links them, not a rule in the database.
- **It was a live bug.** The daily outbox cleanup (`core/outbox/cleanup.py`) deletes
  published events older than 7 days. The foreign key (no `ON DELETE`) made that
  `DELETE` fail as soon as Notification had handled any such event — so since BOOK-51
  the cleanup job would have failed every day once old processed events existed.
  `tests/integration/test_outbox_cleanup.py` pins the fix.

### Its own connection, model catalog and migrations

| Piece | What | Why |
|---|---|---|
| Connection | `notification/db.py` — its own engine + `notification_session()`, on `NOTIFICATION_DATABASE_URL` | The monolith's `UnitOfWork` builds a repository for every Booking table; Notification needs one table |
| Model catalog | `NotificationBase`, its own `DeclarativeBase` | Every table on Booking's `Base` belongs to Booking's DB and migrations. On its own base, Booking's Alembic never sees the Inbox at all |
| Migrations | Its own Alembic setup inside the package (`alembic.ini`, `migrations/`) | A separate database needs its own recipe — one that knows only its own tables |
| Version table | `notification_alembic_version`, not `alembic_version` | If both setups ever run against one database (tests, mid-switch), they can't overwrite each other's version |

### Creating the database: a one-shot `migrate` step

Postgres runs its init scripts (`docker-entrypoint-initdb.d`) **only on a brand-new,
empty volume**. An existing dev `pgdata` volume would never get `notifications` that
way. So `notification/migrate.py` (`python -m notification.migrate`) does both, safely on
every run: create the database if it's missing, then `alembic upgrade head`.

In docker-compose it runs as **`notification-migrate`**, a one-shot service that exits
(`Exited (0)` is success); `notification-consumer` waits on it with
`condition: service_completed_successfully`, so the consumer never starts before its
database is ready. The same pattern as a production pre-deploy migration job. The test
fixtures call the very same script, so it's under test too.

### Rollout: expand, switch, contract

1. **Expand** — the new database and its table exist; the old copy is still there.
2. **Switch** — the consumer's `NOTIFICATION_DATABASE_URL` points at `notifications`.
   Verified live: a booking added rows to the new Inbox while the old one stopped
   changing.
3. **Contract** — Booking's migration `f375ff431cb1` drops the old table.

**Revisit when:** a real deployment exists (separate servers; copy rows before contract).

## 9. Its own package, image and boundary (step 5)

### No imports from Booking — first, while still inside `app/` (5a)

Before moving any file, Notification stopped importing anything from the monolith, so
the move itself was only a move:

- **Its own settings** (`notification/config.py`) with only the four values it uses —
  its DB URL, Kafka, SMTP host and port. No fallback to `DATABASE_URL`: Notification
  doesn't know Booking's database exists.
- **Its own event-type names** (`notification/events.py`) as plain strings, instead of
  importing Booking's `OutboxEventType`. **Separate services share a contract, not
  code**: these exact strings are what Booking writes as `event_type`. If they drift,
  Notification skips the unknown type (§6), no email goes out, and the real-Kafka
  pipeline tests fail.

### Dependencies: a uv workspace, one lockfile

**Chosen:** `services/notification` is a **uv workspace member** — its own
`pyproject.toml` with only its own dependencies (Kafka, SQLAlchemy, asyncpg, Alembic,
pydantic-settings; no FastAPI, Celery, Redis, bcrypt), all resolved into **one shared
`uv.lock`** at the root.

| Option | Why not / why |
|---|---|
| **A. uv workspace, one lockfile** ✅ | One `uv sync` installs both; one CI; the same version of every shared library in both services. The standard monorepo setup for uv |
| B. Fully separate (own lockfile, own venv) | Two environments to sync and keep updated, two CI setups — overhead with no payoff for one developer |

Notification is a **dev-only** dependency of the root project: the cross-service tests
need both packages in one environment, but Booking's code never imports it and Booking's
image (`uv sync --no-dev`) never contains it.

**One trap, caught before it shipped:** with a workspace, uv reads *every member's*
`pyproject.toml` even when it installs none of them. Booking's `Dockerfile` copied only
the root `pyproject.toml` + `uv.lock` before its first `uv sync`, so that step would
have failed ("is not a workspace member"). It now also copies the member's
`pyproject.toml` — only that file, so the dependency layer stays cached across code edits.

### Layout and entrypoints

```
services/notification/
├── pyproject.toml
├── Dockerfile
├── src/notification/       consumer, service, db, models, config, events, migrate,
│   ├── __main__.py         ← python -m notification         (the consumer)
│   ├── migrate.py          ← python -m notification.migrate (one-shot)
│   └── alembic.ini, migrations/
└── tests/                  its unit tests
```

Moved with `git mv`, so each file keeps its history. The empty `router.py` /
`schemas.py` leftovers were deleted — a service with no API needs neither.

### Its own image

`services/notification/Dockerfile`, **built from the repo root** (that's where the
workspace `uv.lock` lives): `docker build -f services/notification/Dockerfile .`. It
installs only `--package notification`. Both compose services (`notification-migrate`,
`notification-consumer`) build from it, and mount **only Notification's own source**
for live edits — the container never sees Booking's code at all.

### The boundary, enforced

`tests/test_service_boundaries.py` parses (never imports) every file of each service
and fails if Booking imports `notification` or Notification imports `app` — including
imports inside function bodies. Checked once against a deliberate violation: it failed
with `events.py imports 'app'`. CI additionally builds Notification's image and asserts
it contains no `app`, `fastapi` or `celery`.

### Where the tests live

| Tests | Where | Why |
|---|---|---|
| Notification's unit tests | `services/notification/tests/` | They test only Notification — they move with it |
| Cross-service tests (outbox → relay → Kafka → consumer → email) | `tests/integration/` at the root | They test the **contract between** the services — splitting them per service would leave nobody checking that the two sides actually match |

A plain `uv run pytest` from the root runs all of them, so CI needed no new test job.

**Revisit when:** Payment (BOOK-65) joins as `services/payment/` — the same layout,
workspace membership, Dockerfile pattern and boundary test (add it to `BOUNDARIES`).

---

# BOOK-65 — Payment as its own service

Flow and open points: `epic-11-flow.md`. This section records what was decided along
the way.

## 10. The starting point: Payment's ties to Booking

| # | Coupling | Where |
|---|---|---|
| 1 | The saga confirms/cancels by calling Booking's functions | `payment/saga.py` — `confirm_reservation()`, `cancel_reservation_system()` |
| 2 | Refunds on cancel are a direct call from Booking's cancel endpoints | `payment/cancellation.py` |
| 3 | Payment reads the reservation from Booking's table | `payment/service.py` — `uow.reservations` |
| 4 | A foreign key into Booking's table | `payments.reservation_id` |
| 5 | Its jobs (reconcile) run in Booking's Celery worker | `app/worker.py` |
| 6 | Same image, same database | `docker-compose.yml` |

## 11. The plan, and where it stands

Same approach as BOOK-64: small steps, still inside the monolith first, the app works
after each.

- **Done:** Payment's own outbox + `payment-events` (§12); Booking consumes it (§13);
  the relay bug and `kafka-init` (§14); a payment for an already-cancelled reservation
  becomes a refund (§15); the frontend polls (§16).
- **Next:** Payment's consumer for `booking-events`; remove the saga's direct calls
  (tie 1); refunds on `BookingCancelled` (tie 2); a sync call + service auth for the
  reservation read (tie 3, BOOK-66 — done, §17); own database (tie 4 — done, §18); extract to
  `services/payment/` with its own scheduler (ties 5–6).

## 12. Payment's own outbox and `payment-events`

**Chosen:** its own `payment_outbox_events` table and its own relay
(`payment/relay.py`), not Booking's `outbox_events` with a topic column. A resolved
**charge** writes `payment_succeeded` / `payment_failed` in the same transaction as its
status — right after charging, and on webhook/reconcile. PENDING (a timeout) and
refunds write nothing: there's no outcome yet, and a refund changes no reservation.

- Own table: it moves with Payment's own database later; no FK to `reservations`.
- Own relay copy: Payment can't import Booking's once extracted.
- Key = `reservation_id`: one reservation's events stay in order. 3 partitions.

## 13. Booking consumes `payment-events`

**Chosen:** a `booking-consumer` process (group `booking`), its own Inbox
(`processed_payment_events`), DLQ `payment-events.dlq`. The Inbox row and the
confirm/cancel are **one transaction** — stronger than Notification's (§7), whose side
effect (an email) can't share one. No retry loop: no flaky external call here.

It only acts on a `pending` reservation. While the saga's direct call still runs, the
event usually arrives after it already confirmed — a no-op, so both paths can run side
by side until the switch-over.

## 14. A bug found: "not failed yet" is not "published"

Both relays marked every row that hadn't *failed* by `flush(timeout=30)`. A message
still waiting then — topic missing, broker slow or down — has had no delivery callback
at all, so it was marked and **lost**. Found when `payment-events` didn't exist.

**Fix:** mark only rows Kafka *confirmed* (`err is None` in the callback); anything
unconfirmed is retried next run. A retry may duplicate a message — fine, every
consumer dedups on `event_id`. Tests use a producer that never confirms; testcontainers'
Kafka auto-creates topics, which is why the bug never showed there.

**`kafka-init`:** auto-create is off on purpose (BOOK-48), but topics were created by
hand once per machine — `payment-events` and both `.dlq` topics never were. A one-shot
compose service now creates every topic with `--if-not-exists`; consumers and the
worker wait for it.

## 15. A payment for an already-cancelled reservation

A charge can resolve *after* its reservation's hold expired (a timeout, reconcile
minutes later). Checking "pending" before charging can't prevent it — across services
it can only be made rare, so it needs compensation.

**Chosen:** Booking's handler writes `booking_payment_rejected` (`payment_id`,
`amount_cents`) to its outbox, in the same transaction as its Inbox row. Payment's
handler (`payment/booking_events.py`) refunds it in full:

- **Amount from Payment's own charge row**, not the event — Payment is the source of
  truth for what it charged.
- **Inbox row added before `create_refund()`**, so it commits together with the PENDING
  refund row, before the provider call. A crash mid-call can't cause a second refund on
  redelivery; reconcile finishes the PENDING one.
- **Inbox rows only for events with an effect** — the other `booking-events` types are
  skipped without one.

`create_refund()` now takes a `reservation_id`, not a `Reservation`.

## 16. The frontend polls the reservation

`BookingForm` and `MyBookings` used to derive the reservation's status from the
payment's response. Once Booking confirms from an event, "payment succeeded" no longer
means "reservation confirmed", so both always poll `GET /reservations/{id}` after
paying. The first check is immediate, so nothing feels slower before the switch-over.

## 17. Reading the reservation before charging: a sync call, not a local copy

Before charging, `POST /payments` needs three things that Booking owns: **who the guest
is** (only they may pay), the **status** (only `pending` is payable) and
**`total_cents`** (how much to charge). Today Payment reads them straight from Booking's
table (`uow.reservations`); with its own database it can't.

**Chosen:** a **sync call** to Booking (`GET /internal/reservations/{id}`, service-auth
per BOOK-66) — not a local copy of reservations kept up to date from `booking-events`.

**Why a local copy works for Notification but not here** — it's not about being a
consumer (Payment consumes `booking-events` too, for refunds); it's about what the data
is *for*:

| | Notification (and Payment's refunds) | Payment, before charging |
|---|---|---|
| What it does | **Reports** something that already happened | **Decides** something now — may this person pay, and how much |
| Data it needs | A snapshot **as of the event** — exactly what the event carries | The **current** state — a few seconds old can be wrong |
| Who's waiting | Nobody; 5 s late is fine | The guest, on the HTTP response |
| When it arrives | After the event, by definition | Often *before* any event could — book, then pay at once |

With a local copy instead:

- **Book-then-pay race:** the guest pays half a second after booking, but `booking_created`
  takes up to ~5 s (relay) to reach Payment → "reservation not found".
- **Stale status:** the hold expired a second ago, Booking cancelled it, the event hasn't
  arrived → Payment's copy still says `pending` and charges a cancelled reservation — then
  refunds it via `booking_payment_rejected` (§15). Money out and back for nothing.

**Rule of thumb:** *react* to something that happened → the event carries the data;
*decide* something now while someone waits → ask the owner synchronously. Same service,
two needs: Payment's refund path uses events, its charge path uses the sync call.

**Accepted trade-off:** if Booking is down, nobody can pay. Acceptable — nobody can book
either. A local copy would let Payment keep charging while Booking is down, which isn't
worth the two problems above.

**Revisit when:** paying must work while Booking is down, or the call's latency becomes a
problem — then a local copy plus a short wait/retry for the book-then-pay race.

## 18. Payment's own database: the model catalog is not the database

Same plan as Notification's (§8) — own database, own migrations, expand → switch →
contract. What's specific to Payment, and one thing that's easy to misread:

**`PaymentBase` doesn't decide which database is used.** Two separate things:

| | Decides | Payment |
|---|---|---|
| The model catalog (`PaymentBase`, `app/payment/db.py`) | Which **migrations** know the tables | Payment's own Alembic |
| The session (engine → URL) | Which **database** queries go to | Booking's `UnitOfWork` → `bookit` until the switch, then `PaymentUnitOfWork` → `payments` |

So moving `Payment`, `PaymentOutboxEvent` and `ProcessedBookingEvent` onto `PaymentBase`
moved **nothing**: the code still queries the same tables in `bookit`.

**A model doesn't have to describe every DB constraint.** The models already dropped
`ForeignKey("reservations.id")` and the `Payment.reservation` ↔ `Reservation.payments`
relationship, while `bookit` still has the FK. That's fine: a query only needs the
**table and column names**, which are unchanged — the generated SQL is identical.
Constraints matter to migrations and to the database itself; the DB keeps enforcing
the FK, and every row the code writes still satisfies it. The model is already in its
future shape; the database catches up at the contract step.

The relationship was a Python-only shortcut (`reservation.payments` → a `SELECT`), never
a column, and nothing used it. Across two databases it couldn't work anyway: Payment gets
the reservation id from the request or the event and stores it on its own rows (§17).

**Two ties it broke loose:**

- `idempotency_keys.payment_id` had a **model-level** FK to `payments` — unresolvable once
  `payments` left Booking's catalog (`NoReferencedTableError`). Removed from the model;
  the column and DB constraint stay until payment keys move to their own table.
- Booking's `alembic --autogenerate` no longer sees the three tables and **proposes
  dropping them** — delete those lines from any generated revision until the contract
  step (warning in `alembic/env.py`).

**Specific to Payment, unlike Notification:**

- **History must be copied** — charges are needed for future refunds; Notification's
  Inbox could start empty.
- **Idempotency keys split out** of the shared `idempotency_keys` into Payment's own
  table — which also removes a latent collision: one key column for both reservation and
  payment keys.
- **A unit of work, not a bare session** — Payment's code already passes `uow` through
  its layers (`uow.payments`, `uow.session`); `PaymentUnitOfWork` keeps that shape.

### Copying the history (`app/payment/copy_from_booking.py` — deleted at contract, see git history)

**Chosen:** a one-off module, not part of `migrate.py` — `migrate.py` is permanent, the
copy only exists during the move and is **deleted at the contract step**.

| From `bookit` | To `payments` |
|---|---|
| `payments` | `payments` — all rows |
| `idempotency_keys` | `payment_idempotency_keys` — only rows with a `payment_id`; reservation keys stay |
| `payment_outbox_events` | `payment_outbox_events` — all, **including unpublished** |
| `processed_booking_events` | `processed_booking_events` — all |

- **Repeatable:** every insert is `ON CONFLICT DO NOTHING`, so a second run copies nothing.
  Run it now, and once more at the switch.
- **All or nothing:** one transaction on the target for all four tables.
- **Order:** `payments` before `payment_idempotency_keys` (its FK).
- **Plain SQL over asyncpg**, no ORM — two connections, rows in, rows out.
- **Unpublished outbox rows** go too: after the switch Payment's relay sends them from the
  new DB. If the old relay already sent one, Kafka gets a duplicate — Booking's Inbox
  skips it.

**At the switch:** a payment written between the last copy and the code switch would be
missed. So: stop `app`, `worker` and `payment-consumer`, run the copy a final time, start
them on the new code. A few seconds of downtime in dev. **Revisit when:** downtime isn't
acceptable — then dual writes or change-data-capture (CDC) instead of a stop-and-copy.

**Checked on dev:** 30 payments, 4 payment keys, 5 outbox events, 1 Inbox row copied; a
second run copied 0; a checksum over every `payments` row matches in both databases.

**Done on dev:** stop → final copy (0 new rows, checksums equal) → start on the new
code; a live payment then landed only in `payments` (31 rows vs. 30 frozen in
`bookit`). Contract: migration `8b4228f69ed7` dropped the old tables, the
`idempotency_keys.payment_id` column and the three enums from `bookit`; the copy
module was deleted. Booking's `alembic check` is clean again.

### What production would do instead

Same plan (copy → switch → contract later); the copy and switch must stop payments for
seconds at most, or only in a planned window.

| Approach | How | Pause | Cost |
|---|---|---|---|
| **Maintenance window** (what dev does) | Announce it, stop payment endpoints, copy, verify, start on new code | Minutes, grows with data | Simplest, safest |
| **Postgres logical replication** | `bookit` *publishes* the tables, `payments` *subscribes* — changes stream continuously; at cutover pause writes, wait to catch up, switch, end the subscription | Seconds | Setup + permissions; Postgres → Postgres only |
| **Dual writes / CDC** | Code writes to both DBs (or a CDC tool such as Debezium replays the old DB's change log), backfill old rows, switch reads, then stop writing the old one | None | Most work; two writes can drift apart; more infrastructure |

**For BookIt's size:** logical replication — or a short window at night, if minutes of
downtime are fine.

**Also different in production:**

- **Separate database servers**, not two databases on one Postgres — Payment's gets its
  own server, backups and access rules (part of why Payment splits at all: PCI-DSS, §0).
- **Verify before switching** — row counts and a checksum per table, like the md5 above.
- **A rollback plan** — the old tables stay until there's confidence; that's why
  *contract* is its own, later step (sometimes weeks later).
- **A feature flag for the switch** — point the code back at the old database without a
  redeploy if something goes wrong.

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| What gets split out | Only Notification and Payment; users / properties / reservations / reviews stay together in Booking | Part of Booking develops its own reason to split |
| BOOK-64 repo structure | `services/notification/` in the same repo (monorepo) | Separate owners want separate release cycles |
| "Reservation received" trigger | Kafka `BookingCreated` via the outbox — revisits `worker-decisions.md` §9 | Never back to Celery while Notification is separate |
| Why not a Celery task across services | A command makes the sender know the receiver; an event lets the receiver know the sender | — |
| Email data | Carried in the event payload, no lookups into Booking — reverses BOOK-49's thin payload | Payload grows too large or too sensitive |
| `_emit_outbox_event` | `async`, loads guest + property to fill the payload | — |
| Changing a message path | Expand and contract: add the new path, switch the sender, then delete the old | — |
| Rolling out a new event type | Receiver learns it first, then the sender starts sending | — |
| Unknown event type at the consumer | Log a warning and skip — never a crash (poison message) | — |
| Event payload | A cross-service contract: add freely, never remove/rename first | — |
| Consumer crashes | `restart: unless-stopped`; Kafka redelivers the uncommitted message | — |
| Temporary SMTP error | Retry in place: 1, 2, 4, 8 s, then dead-letter | Retries holding the partition becomes a problem |
| Permanent SMTP error | No retry, straight to the DLQ — checked *before* `OSError`, which every smtplib error subclasses | — |
| Message that fails for good | `booking-events.dlq`: original bytes + error headers, then commit; DLQ publish fails ⇒ don't commit | Failures frequent enough to need a replay tool or alert |
| Notification's database | `notifications` on the same Postgres server; Inbox started empty | A real deployment: separate servers, copy rows before contract |
| Cross-service foreign key | Dropped — the shared `event_id` *value* links the services; also fixed the outbox cleanup failing on it | — |
| Notification's tables | Own `NotificationBase`, own Alembic setup, own version table `notification_alembic_version` | — |
| Creating its database | `python -m notification.migrate` (create if missing + upgrade), one-shot `notification-migrate` before the consumer | — |
| Event-type names | Notification's own constants — a shared contract, not shared code | — |
| Dependencies | uv workspace member, one shared `uv.lock`; dev-only dependency of the root | Separate owners need separate release cycles |
| Notification's image | `services/notification/Dockerfile`, built from the repo root, installs only `--package notification` | — |
| Service boundary | `tests/test_service_boundaries.py` (AST scan both ways) + CI image check | Payment joins: add it to `BOUNDARIES` |
| Cross-service tests | Stay at the root `tests/integration/` — they test the contract between the services | — |
| BOOK-65 Payment's outbox | Own `payment_outbox_events` + own relay → `payment-events` (3 partitions, key `reservation_id`) | — |
| Which payments emit an event | Only a resolved charge (`payment_succeeded` / `payment_failed`); not PENDING, not refunds | Something needs refund outcomes |
| Booking's `payment-events` consumer | Group `booking`, Inbox + confirm/cancel in one transaction, DLQ `payment-events.dlq`, no retry loop | — |
| Saga's direct calls | Still run beside the event path until the switch-over | Removed once "can't use this payment" refunds work end to end |
| Relay marking | Only Kafka-*confirmed* rows are marked published (both relays) | — |
| Kafka topics | Created by the one-shot `kafka-init` service, `--if-not-exists`; auto-create stays off | — |
| Payment for a cancelled reservation | Booking emits `booking_payment_rejected` → Payment refunds in full, amount from its own charge row | Prevention (don't expire mid-payment) is worth adding too |
| Payment's Inbox | `processed_booking_events`, committed with the PENDING refund row, only for events with an effect | — |
| Frontend after paying | Always poll the reservation, first check immediate | Push (SSE/WebSocket) if the wait gets noticeable |
| Payment reading the reservation before charging | Sync call to Booking (`/internal/...`, BOOK-66 auth), not a local copy from events — it *decides* on current state while the guest waits | Paying must work while Booking is down |
| Payment's tables | Own `PaymentBase` + own migrations; models drop the FK/relationship first, the DB catches up at contract | — |
| Payment's DB access | `PaymentUnitOfWork` (same `uow.payments` / `uow.session` shape), not a bare session | — |
