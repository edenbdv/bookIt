# What BookIt chose for background work, and why

The companion to `background-jobs-basics.md`. That doc is general — when each option is
the right one. This one records which we picked, what we rejected, and the condition
under which each decision should be revisited.

**Status: decided, not yet built (`BOOK-44`).** Written before implementation so the
topology is deliberate rather than discovered. Anything marked *provisional* should be
confirmed against the code once it exists.

**Current reality, since it shapes several of these:** one machine, one process. The
app runs on the host with `uvicorn --reload`; `docker-compose.yml` runs only `db`,
`redis` and `rabbitmq`. There is no deployment yet. So the multi-instance reasoning
below is about what the design must *not preclude*, not a problem we have today.

## 1. A worker process, not a loop or cron

**Chosen:** a dedicated worker process consuming a queue.

**The honest version:** the first job we need — expiring pending reservations — would
be served perfectly well by an `asyncio` loop with a `try/except` per iteration. It's
idempotent, low-stakes, and we run one instance. If it were the only job, this decision
would go the other way and the ladder in `background-jobs-basics.md` §2 says so.

The case rests on the two jobs immediately behind it:

- **Sending a confirmation email is triggered, not timed.** It has no cron shape and
  no loop shape. It needs a queue.
- **Retry with backoff** needs "attempt 2 of 5, next attempt at 14:32" to survive a
  restart, plus somewhere for jobs that exhaust their attempts. A loop has nowhere to
  put either.

So the alternative isn't "loop instead of worker" — it's "loop *and* a queue", i.e. two
mechanisms where one would do, with the periodic half getting none of the retry or
visibility the other half has.

**Rejected, with the reason it doesn't fit:**

| | Why not |
|---|---|
| `BackgroundTasks` | Handles the email, but a restart destroys it with no record it was meant to happen. Nothing for scheduled work |
| In-process loop | Fine for the expiry job alone; nothing for triggered work or retries |
| OS cron / Task Scheduler | Right tool for ops work, wrong for triggered work. A `.bat` locally is also not what a Linux container would run, so we'd test a different mechanism than we deploy |

**Revisit if:** the email and retry tickets are dropped or deferred indefinitely. Then a
loop is the correct, smaller answer and this is over-built.

## 2. Celery + RabbitMQ, not arq + Redis — *revised, supersedes the original §2/§3*

**Chosen:** Celery, with RabbitMQ as broker.

**Original reasoning (kept for the record, and it wasn't wrong on its own terms):**
async-native, scheduling built into the worker instead of a separate `beat` process,
and a smaller surface to read. All still true of arq. None of it is why this changed.

**Why revised:** this project is meant to demonstrate depth, not just ship a working
queue. arq + Redis is a fully legitimate architecture for a smaller async app — the
switch isn't "arq is unreliable." It's that Celery + RabbitMQ is the more conventional
broker + task-framework pairing, and building it means learning mechanisms arq
abstracts away entirely: queue semantics, acknowledgements, redelivery, routing,
dead-lettering. That's worth more here than the smaller surface arq offered.

**What this buys, stated precisely — this is the part worth being exact about, because
it's easy to overclaim:**

- **RabbitMQ owns message *delivery*.** It holds a task until a worker acks it, and
  redelivers if a worker dies mid-processing before acking. That's broker-level fault
  tolerance for an **infrastructure failure** (worker crash, dropped connection).
- **Celery owns task *execution and retry policy*.** Whether a task that was delivered
  fine but failed for a business reason (the email provider timed out) should be
  retried, how many times, with what backoff — that's an **application-level** decision,
  made by Celery, not RabbitMQ. RabbitMQ redelivering an un-acked message and Celery
  retrying a failed-but-acked task are different mechanisms for different failure modes.
- **RabbitMQ does not "provide retries."** Celery's retry/backoff API
  (`autoretry_for`, `retry_backoff`, `max_retries`) works the same way regardless of
  broker — Celery-on-Redis gets it too. The reason to pick Celery over arq is a more
  mature retry/workflow API; the reason to pick RabbitMQ over Redis is delivery
  guarantees. They're two separate axes, not one bundled justification.

**What we give up, honestly:**

- **Async-native simplicity.** Celery is sync-first; the app is async end to end. A
  worker needs its own way to get a database session — `get_db()` is request-scoped and
  doesn't apply outside a request. arq's answer would have been "just use
  `UnitOfWork()` directly, nothing new needed"; Celery's answer isn't that simple and is
  now the most important open question in §6.
- **A second process type.** Celery needs `celery beat` for the scheduled expiry job
  (BOOK-46) — arq would have run scheduling inside the worker itself. Two process types
  to run and monitor instead of one, and beat does not deduplicate itself.

**Revisit if:** the sync/async bridge costs meaningfully more than the learning is
worth, or `celery beat` deployment friction is disproportionate to two jobs.

## 3. RabbitMQ for the task queue; Kafka for the event bus — two brokers, deliberately

**Chosen:** RabbitMQ is the Celery broker here (Epic 6). Epic 7's event bus
(BOOK-48) uses **Kafka**, not RabbitMQ. Redis stays for caching, rate limiting, and
distributed locks — it is not used for jobs or events.

This means the project runs three messaging-adjacent components, each doing the thing
it's actually suited to, rather than one broker stretched across two purposes:

- **RabbitMQ / Celery — task queue.** "Run *this function* later." Celery owns the
  queue; there's no topology to design beyond what Celery creates. Strong at routing,
  per-message ack/nack, and dead-lettering a single failed job.
- **Kafka — event bus.** "*This happened*," published once, replayable, read by
  however many independent consumers want it at their own pace. This is what BOOK-48
  designs — topics and partitions, not exchanges and bindings — and it's what makes the
  **outbox pattern** (Epic 7) natural: the relay reads an append-only log instead of
  publishing into a broker that doesn't retain what's already been consumed.
- **Redis — cache, rate limiting, locks.** Fast, disposable, ephemeral state. Never a
  queue for anything that must not be lost.

Because these are two genuinely separate brokers rather than one broker shared for two
purposes, there's no topology-collision risk to design around here — no vhost, no
shared exchange namespace. `docker-compose.yml` simply runs both `rabbitmq` and `kafka`
as independent services.

**Revisit if:** running a third piece of infrastructure (Kafka) purely for the event
bus turns out to be more operational overhead than the learning is worth, in which case
collapsing the event bus onto the existing RabbitMQ (per BOOK-48's original title) is
the fallback — but that would need the vhost/namespace separation this version avoids.

## 4. One worker, many jobs

**Chosen:** one worker process registering every job function. Scale by running more
*copies of the same worker*, never a worker per task.

Splitting into separate workers or queues waits for a concrete pressure —
head-of-line blocking, differing resource profiles, differing scaling curves, blast
radius, or priority (`background-jobs-basics.md` §5). None apply with two jobs.

**Revisit if:** any one of those five actually bites. Record which one, in this file.

## 5. The expiry job goes first

**Chosen:** build the worker, then the expiry job, then retries, then email.

Not because expiry is the most valuable, but because it's the **safest place to
discover the infrastructure is wrong**. It's idempotent, depends on nothing but the
database, and if it doesn't run for a day nobody loses money. It exercises the whole
pipeline — scheduling, a `UnitOfWork` inside a worker, a commit — with no external
service in the loop.

Building the worker against the email job instead would mean debugging the worker and
the mail transport simultaneously, with no way to tell which is broken.

Retries come before email for a related reason: a retry policy is much easier to design
against a real job that can fail than in the abstract.

**Implementation note, now that this is built — where each piece actually lives:**

```
app/
├── worker.py
│   └── expire_pending_reservations()   ← the Celery task itself
│
├── reservation/
│   ├── repository.py
│   │   └── list_expired_pending()      ← DB query, SELECT ... FOR UPDATE
│   └── service.py
│       └── transition_to()             ← state-machine legality check
│
└── core/
    ├── db/
    │   └── unit_of_work.py             ← UnitOfWork, session, commit/rollback
    └── jobs/
        └── celery_app.py
            ├── app = Celery(...)
            └── async_task(...)         ← sync/async bridge (§2's open question)
```

Deliberately spread across existing module boundaries rather than gathered into one
"jobs" file: `list_expired_pending()` sits with the rest of `ReservationRepository`
because it's a query about reservations, not about jobs; `transition_to()` was already
`reservation/service.py`'s job before this ticket existed. The task itself
(`worker.py`) is the one place that's genuinely *about* being a scheduled job — it just
calls out to code that already knew how to do the actual work.

**The call flow, end to end:**

```
Celery Beat (fires on schedule, §2/§9)
     │
     ▼
expire_pending_reservations()            -- app/worker.py, the task
     │
     ▼
async with UnitOfWork()                  -- opens one transaction
     │
     ▼
repository.list_expired_pending()        -- SELECT ... FOR UPDATE, locks matching rows
     │
     ▼
for each Reservation:
  transition_to(reservation, CANCELLED)  -- in-memory only; no DB write yet
     │
     ▼
UnitOfWork.__aexit__ -> session.commit() -- the only DB write: flushes every
                                             changed .status at once, then COMMIT
                                             ends the transaction, releasing every
                                             lock acquired above, together
```

The locks from `FOR UPDATE` aren't released row-by-row as each is processed — they're
all held until this single commit at the very end of the loop. Safe here because
`transition_to()` is pure Python with no I/O; holding a lock across a slow *external*
call is the case `concurrency-basics.md` §6 warns about, and doesn't apply to this job.

## 6. Open questions for `BOOK-44`

- **How does a Celery task get a database session, given the app is async?**
  `get_db()` (`core/db/engine.py`) is a request-scoped, async FastAPI dependency and Celery
  tasks are sync by default. Needs a concrete answer — e.g. a separate sync
  SQLAlchemy engine/session for the worker, or running the async `UnitOfWork` via
  `asyncio.run()` per task — before any real job is written. This is the main cost of
  choosing Celery over arq and should be resolved first.
- **Confirm `celery beat`'s scheduling deduplication.** Unlike arq's in-worker cron,
  beat is a separate process; if more than one instance of it ever runs, the expiry
  job fires more than once. Needs an explicit answer for "run exactly one beat," not
  an assumption.
- **Does the worker ship in the same image as the app?** Same `Dockerfile` with a
  different command is the simpler default.

## 7. BOOK-47 reordered after BOOK-45 — the expiry job turned out not to need retry

**Original plan (§5): retry before email**, so the policy would be designed against a
real job rather than in the abstract. Once BOOK-46 actually existed, that reasoning
didn't hold up:

**The expiry job already self-heals via its own schedule, for free.** A transient
failure (the DB briefly unreachable, say) commits nothing — so the exact same
reservations (still `pending`, still past `expires_at`) match the query again on the
*next* beat tick, 60 seconds later, with no retry code involved at all. This falls out
of three properties the job already has: it's **idempotent**, it's **independently
re-triggered** (each tick is its own attempt, not a continuation), and a failure
**never partially commits**. Adding `autoretry_for`/backoff on top of that wouldn't add
real robustness — it would be infrastructure solving a problem the schedule already
solves.

**Email has the opposite shape.** It's triggered once, per booking — there's no "next
tick" that naturally re-attempts a failed send. Without retry, a failed send is simply
gone forever; nothing else in the system will ever try again. That's a real gap
BOOK-46 never had.

**So BOOK-47 moves after BOOK-45, and gets designed against email instead of expiry** —
the job that actually needs retry protection, and that has genuine external failure
modes (SMTP timeout, provider errors) to build against instead of synthetic/injected
ones.

**One limit worth naming, so it isn't confused with what retry actually fixes:** this
self-healing argument only covers *transient* failures. A genuine bug (a broken query,
a bad migration) fails every tick identically forever — retry wouldn't help there
either, per BOOK-47's own rule ("retry transient failures, not permanent ones"), and
neither would dead-lettering, since there's no burst of failed attempts to catch, just
a steady one every 60 seconds. That's a monitoring/alerting problem (Epic 9), not a
retry-policy one.

**Revisit if:** the expiry job ever gains a step that isn't idempotent or that partially
commits — at that point the self-healing argument stops applying and a real retry
policy would be worth adding here too.

## 8. Mail transport for BOOK-45: MailHog now, a provider later — never a self-hosted server

**Chosen:** MailHog (or Mailpit) as a local SMTP test double for now. A real
transactional provider (SES, Postmark, SendGrid) is deferred until this project needs
something to run in an actual deployment. Full background: `email-basics.md`.

**Why the code doesn't need to know which one it's talking to:** `notification/service.py`
owns "compose and send" as one function; whether that function opens an SMTP connection
to a local container or calls a provider's API is contained entirely inside it. Neither
`app/worker.py` (the task) nor whatever triggers it changes when this decision is
revisited — same "only the trigger changes" boundary as §2, applied one layer over.

**Rejected, with the reason it doesn't fit:**

| | Why not |
|---|---|
| A self-hosted SMTP server (Postfix, etc.) | Starts every deliverability problem from zero — no sending reputation, no SPF/DKIM/DMARC set up — and it's ongoing operational work unrelated to what this app does. `email-basics.md` §2 |
| A real provider, right now | Needs an account and an API key/secret in `.env`, and every dev-loop test send would count against a rate limit. Also makes triggering a failure on demand (needed for BOOK-47) harder than stopping a local container |
| Sending to a real inbox (e.g. personal Gmail) during development | Needs a real recipient per test run, and personal-account SMTP is rate-limited and not meant for automated app sending |

**Revisit if:** this project ever needs a real deployment where someone other than the
developer receives the email — at that point MailHog stops being sufficient, not
because it broke, but because it was never meant to deliver anything.

## 9. BOOK-45's trigger: a direct Celery call, permanently — `BookingCreated` never goes to Kafka

**Chosen:** `create_reservation_idempotent()` enqueues the email via `UnitOfWork.on_commit()`
firing a Celery `.delay()` call, the same shape as every other Epic 6 job. **This is the
permanent mechanism for this job, not a placeholder** — unlike the original version of
this section, `BookingCreated` does not migrate to Kafka when Epic 7 lands. Only
`BookingConfirmed`/`BookingCancelled` do (BOOK-48/51).

**The coupling this creates, honestly:** a direct call means `reservation/service.py`
imports and calls into `app.worker` by name — Reservation "knows about" Notification.
Real, but proportionate: exactly one consumer exists for "a reservation was created,"
checked against the actual backlog (Epic 7 through Epic 9) — BOOK-48's "future
consumers (analytics, audit log, whatever comes later)" is a capability kept open, not a
planned ticket.

**Why not Kafka anyway, once Epic 7 exists — this is the actual, final answer, not a
temporary one:** sending one transactional email in reaction to one action is the
textbook case for a task queue, not an event bus — standard industry practice (Stripe,
Shopify, and most systems trigger transactional email via a direct background-job
enqueue, not an event pipeline). Routing this through Kafka anyway, purely so
Notification is architecturally "pure" (one trigger mechanism for every job), is the
well-known anti-pattern of forcing every side effect through an event bus just because
one exists elsewhere in the system. An event bus earns its cost where multiple
independent things need to react to a fact; a single email doesn't have that shape, and
manufacturing hypothetical future consumers to justify the migration isn't a real reason
(see the "does not justify" point above).

**The ordering gap this raised, and how it's actually closed:** `get_uow()`
(`core/db/unit_of_work.py`) commits the reservation *after* the route handler returns,
so a naive `.delay()` fired from inside `create_reservation()` would always precede that
commit. `UnitOfWork.on_commit()` fixes this: it's a hand-rolled version of a standard,
named pattern — Django's `transaction.on_commit()`, Rails' `after_commit`, SQLAlchemy's
own session-commit events all exist for exactly this reason. It queues a callback that
only fires once `session.commit()` succeeds, so the email can no longer be enqueued for
a reservation that failed to commit.

**What `on_commit()` still doesn't cover, accepted permanently for this job:** the
callback lives only in this process's memory — a crash in the narrow window between
commit succeeding and the callback running still loses it silently, with no record. A
durable outbox (the fact written in the same transaction, a relay publishing it
separately) is the only thing that closes that. For `BookingCreated` specifically, that
residual risk is accepted for good, not deferred to a future ticket: losing one
"request received" email to a crash landing in a vanishingly narrow window is low-stakes
enough that it doesn't justify Kafka + an outbox table + a relay on its own.

**A second, separate gap `on_commit()` doesn't cover either — confirmed by actually
stopping RabbitMQ and watching it happen:** `on_commit()` only guarantees the callback
*runs*, not that the `.delay()` call inside it *succeeds*. With the broker down,
`.delay()` raised `kombu.exceptions.OperationalError`; the hardened `__aexit__` (below)
caught and logged it, so the request still returned `201` and the reservation stayed
committed — but the message was never published, and nothing retries that. That
reservation's guest never gets a confirmation email, silently, with only a log line as a
record. This is a **third distinct failure mode**, not the same as either gap above:

| | What fails | What's already handled |
|---|---|---|
| Ordering (fixed) | Enqueuing before the transaction commits | `on_commit()` |
| Crash durability (accepted gap) | Process dies between commit and the callback running | Nothing — needs an outbox |
| **Publish failure (accepted gap, newly confirmed)** | **The broker is unreachable when `.delay()` is called** | **Nothing — logged, not retried** |

**Why this matters for BOOK-47 specifically:** a task-level `autoretry_for` decorator
(BOOK-47's stated mechanism) only retries a task that already exists in Celery's
registry and started running — it does nothing for a `.delay()` call that never
successfully published in the first place, since no task was ever created to retry.
BOOK-47 needs to explicitly account for the *publish* step failing, not only the *task
body* failing — two different call sites, two different try/except blocks needed.

**Why `BookingConfirmed`/`BookingCancelled` are different, and do go through Kafka:**
those are tied to Epic 8's real payment state changes, where durability has more at
stake and other consumers plausibly matter. Notification consumes those as a Kafka
consumer group; `BookingCreated` is never published as a Kafka event at all. These are
two genuinely different, permanent mechanisms for two different jobs — not a migration
in progress.

**Revisit if:** a second real consumer of "a reservation was created" actually gets
planned — at that point the coupling cost (not the ordering gap, which `on_commit()`
already handles) would justify moving this specific trigger to the event bus too.

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| Where work runs | A worker process on a queue | Triggered work and retries both get dropped |
| Library | Celery (revised from arq) | Sync/async bridge cost outweighs the learning goal |
| Task broker | RabbitMQ (revised from Redis) | Delivery/redelivery guarantees stop being worth the ops cost |
| Task broker vs event bus | Different brokers — RabbitMQ (tasks) vs. Kafka (events) | Running Kafka costs more than the event-bus learning is worth |
| Worker count | One worker, many jobs; scale with copies | Head-of-line blocking, resources, scaling, blast radius, priority |
| First job | The expiry job — idempotent, no external deps | — |
| Retry policy target | Email (BOOK-45), not expiry (BOOK-46) | The expiry job gains a non-idempotent or partially-committing step |
| Mail transport | MailHog (local test double), not a real provider yet | A real deployment exists where someone besides the developer receives mail |
| BOOK-45 trigger reliability | `uow.on_commit()`, not a direct `.delay()` — fixes ordering, not crash durability | A second real consumer of "reservation created" is planned before Epic 7 lands |
| `BookingCreated`'s trigger, long-term | Stays on Celery permanently — never migrates to Kafka | A second real consumer of "reservation created" actually gets planned |
