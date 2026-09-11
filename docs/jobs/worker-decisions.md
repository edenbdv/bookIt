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
BOOK-47's own resolution for this: don't retry it at all (retrying here would run
*inside* the guest's request — retry-basics.md §6 — turning a broker blip into the
guest waiting on their `201`, exactly what this ticket exists to prevent). Caught,
logged, request unaffected — final, not a placeholder.

**The crash-durability and publish-failure gaps can't be dead-lettered either, structurally
— not an oversight, an actual ceiling.** `notifications.dlq` (BOOK-47 §10 above) is
itself a RabbitMQ queue. If the reason `.delay()` failed is "RabbitMQ is unreachable,"
publishing a dead-letter record to another RabbitMQ queue fails for the identical
reason — there's no broker-based fix for a broker-down failure. The only thing that
could give either gap real durability is writing the intent somewhere that isn't
RabbitMQ at all — the database, in the same transaction as the reservation — which is
exactly what an outbox is. There is no intermediate option here between "accept the
gap" and "build an outbox"; nothing smaller closes it.

**Why accepting both gaps for this job, specifically, holds up — the complete case:**

1. **What's actually lost is small.** The guest already has their `201`; the reservation
   is real and visible to them. The only casualty is a secondary courtesy email
   confirming what they already know happened.
2. **The probability is narrow.** Needs the broker down in the *exact* window of one
   request's `.delay()` call, or a crash in a window measured in milliseconds — not "the
   broker is flaky," but a precise, rare overlap for a single, health-checked service.
3. **The real fix costs real, standing infrastructure.** A table, a relay process, its
   own dedup logic, and a new thing to monitor for health — not a small addition.
4. **Even with an outbox, nothing is perfectly durable** — the failure point just moves
   to "is the relay itself healthy." Diminishing returns on an already low-severity risk.
5. **Consistent with how this whole project draws this line, not a one-off excuse.**
   BOOK-44's worker waited for two real jobs to justify it; retry (BOOK-47) targeted
   email, not expiry, because expiry didn't actually need it. Same rule here: build
   infrastructure once what it protects is worth protecting, not ahead of that.
6. **The contrast makes the line legible.** `BookingConfirmed`/`BookingCancelled` *do*
   get an outbox (BOOK-50) — those carry real payment-adjacent consequences (Epic 8), where
   a silently-lost event is a genuine consistency problem. A missed "we got your
   request" email carries none of that weight. Same tool, applied only where the stakes
   clear the bar.

**Why `BookingConfirmed`/`BookingCancelled` are different, and do go through Kafka —
three reasons, and the load-bearing one isn't consumer count:**

1. **Stakes, not consumer count — this is the real dividing line.** Neither job has a
   second real consumer *today*; BOOK-51's Notification consumer group is currently the
   only planned reader of `booking-events`, exactly as `BookingCreated`'s email is the
   only reader of its trigger. What actually differs is what losing the event silently
   costs: these two carry Epic 8's real payment-adjacent state — a silently-lost
   `BookingConfirmed` is a genuine consistency problem (payment succeeded, nothing ever
   knew). A missed "we got your request" email is not. Same tool (outbox + durable
   transport), applied only where that cost clears the bar — "future consumers" is a
   capability kept open, not the actual justification.
2. **A real ordering requirement between the two event types themselves** — a distinct
   thing from the enqueue-ordering gap `on_commit()` fixes above. `BOOK-38` lets a guest
   cancel a reservation that's already `CONFIRMED`, so `BookingConfirmed` and
   `BookingCancelled` can both happen, for the same reservation, over time. A consumer
   holding any derived state (e.g. "is this reservation currently active") gets the
   wrong answer if it processes them out of order. Kafka's partition key
   (`reservation_id`, `kafka-basics.md` §3) makes that ordering a structural guarantee,
   not a hope. **`BookingCreated` never faces this problem in the first place** — it's
   not one of a set of events that can race each other for the same reservation; a
   reservation must exist (Created) before it can be confirmed or cancelled at all, so
   causally it's always first, by construction, regardless of transport.
3. **Multiple independent consumers becoming real is a plausible, not planned, future**
   — kept open by using Kafka, not the reason for using it today.

**Could RabbitMQ provide reason 2's ordering guarantee instead?** Only approximately,
and not natively:
- One queue, one consumer (`concurrency=1`) gives true global FIFO — but serializes
  *every* event for *every* reservation through one consumer, killing the parallelism
  partitioning exists to keep.
- One queue, multiple consumers (the competing-consumers shape `BOOK-44` already uses)
  gives no ordering guarantee at all — two consumers can finish out of the order they
  received messages in.
- The `consistent-hash-exchange` plugin (non-default) can route same-key messages to
  the same queue, similar in spirit to a partition — but still needs exactly one
  consumer *per queue* to preserve order within it, which is manually reconstructing
  Kafka's partition model with an add-on plugin, not a built-in guarantee. And even
  then, RabbitMQ still deletes a message once acked (`rabbitmq-basics.md` §4) — no
  retention/replay, and no native multiple-independent-consumer-groups mechanism either.

Per-key ordering is a structural difference between a log and a queue, not a vocabulary
difference — §3 above's "different vocabulary" framing was about producers/routing,
not this.

**Revisit `BookingCreated` if:** a second real consumer of "a reservation was created"
actually gets planned (reason 3 above stops being hypothetical) — reasons 1 and 2 don't
change with time, they're structural to what this specific event is.

## 10. BOOK-47: retry the task body, deliberately not the publish call

**Chosen, following retry-basics.md's job-retry-vs-enqueue-retry distinction (§5):**
only the first of the two failure modes §9 identified gets a retry policy.

**Task-body retry** (`app/worker.py`, on `send_reservation_received_email`):

```python
@app.task(
    autoretry_for=_TRANSIENT_SMTP_ERRORS,  # OSError, SMTPConnectError,
                                            # SMTPServerDisconnected, SMTPHeloError
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
```

**Which (transient vs permanent), concretely:** `OSError` (covers connection refused,
timeout, and DNS failure — all three showed up as `gaierror`, an `OSError` subclass, in
testing) plus `SMTPConnectError`/`SMTPServerDisconnected`/`SMTPHeloError`.
`SMTPRecipientsRefused`/`SMTPSenderRefused` (a bad address) are deliberately excluded —
retrying doesn't fix a bad address, per retry-basics.md §1.

**Verified live, not just read from the decorator:** stopped MailHog, created a
reservation, watched the same task ID retry with growing, jittered backoff, then
started MailHog back up:

```
13:13:27  retry: Retry in 1s   (attempt 1, MailHog still down)
13:13:36  retry: Retry in 0s   (attempt 2, jitter shrank the wait)
13:13:44  retry: Retry in 0s   (attempt 3)
13:13:52  retry: Retry in 4s   (attempt 4, backoff growing)
13:13:56  succeeded            (attempt 5, MailHog back — sent)
```

Same task ID throughout (Celery's own log line includes it) — one task retrying, not
five separate ones.

**What happens once `max_retries` is exhausted — and a real surprise building it.**
The first instinct is `rabbitmq-basics.md` §6's dead-letter exchange — broker-native,
nothing custom to build. **It doesn't actually apply here, and that's worth being
precise about:** RabbitMQ's DLX only fires on a *nack* (or TTL/queue-length), and Celery
**ACKs a task the moment it finishes running, success or failure** — exhausting
`max_retries` still ends in an ordinary ACK, not a nack. A queue-level DLX would sit
there forever unused; there's nothing to trigger it. This is the same category of
mismatch as `rabbitmq-basics.md` §4's "RabbitMQ does not provide retries" — two features
that sound like they'd compose and don't, because they're solving different layers
(broker-level delivery vs. application-level task outcome).

**What actually works: Celery's `task_failure` signal** (`core/jobs/celery_app.py`) —
fires exactly once a task's outcome is *finally* failure, whether that's an exception
outside `autoretry_for` or every retry used up. The handler manually publishes task
name, id, args/kwargs, exception, full traceback, retry count, and a timestamp to a
plain durable queue (`notifications.dlq`) via the same RabbitMQ connection Celery
already holds — inspectable in the management UI, no new system. Verified live: stopped
MailHog, let all 5 retries exhaust, and found the exact reservation id (plus a full
traceback and `"retries": 5`) sitting in `notifications.dlq`.

**Publish-call failure — deliberately not retried, not a gap left by oversight.** Per §9's
"third failure mode": retrying `.delay()` itself would mean retrying *inside*
`on_commit()`'s callback, which runs as part of the guest's own request
(retry-basics.md §6 — retry that blocks). Adding retries there would make a broker
outage turn into the guest waiting several seconds-to-tens-of-seconds for their `201`,
which is exactly what BOOK-45 exists to prevent. The existing behavior (caught, logged,
not retried, request unaffected) stays as-is — confirmed separately in §9 by actually
stopping RabbitMQ.

## 11. BOOK-50: polling relay, not CDC

**Chosen, following outbox-basics.md §4's generic comparison table.** CDC (e.g.
Debezium) wins unambiguously on latency and query efficiency — but that win is bundled
with real, separate costs that don't fit this project:

- Postgres would need `wal_level=logical` plus a replication slot — an actual
  server-config change, not just an app-level dependency.
- Debezium itself needs a place to run — typically Kafka Connect — which is another
  service to deploy, monitor, and keep healthy, on top of everything already in
  `docker-compose.yml`.
- It's a whole new category of infrastructure (connectors, connector offsets, its own
  failure modes) with no overlap with anything already built here.

None of that latency actually matters for what BOOK-49 publishes: a `BookingConfirmed`/
`BookingCancelled` event feeding a notification email. A few seconds of poll-interval
delay before that email goes out is not a correctness problem, and not something a
guest would ever notice. The polling relay's real downsides (added latency, repeated
empty queries) are the pattern's actual cost *here* — and both are already priced in and
handled (the delay is fine for this use case; the empty-query cost is what §6's partial
index exists to keep cheap).

**Concretely:** a Celery beat periodic task, same shape as BOOK-46's expiry job —
queries `outbox_events WHERE published_at IS NULL ORDER BY created_at` (creation order
matters for the per-`reservation_id` ordering guarantee, outbox-basics.md §6),
publishes each to Kafka, sets `published_at`.

**Revisit when:** a real deployment exists where a several-second delivery delay
genuinely matters for some event this project starts publishing — CDC's cost only pays
for itself once latency at that scale is actually worth something.

---

## 12. BOOK-51: a standalone Kafka consumer process, not a Celery task

**Chosen:** `app/notification/consumer.py` — a `confluent_kafka.Consumer` in group
`notifications`, subscribed to `booking-events`, run as its own long-running process
(its own `docker-compose.yml` service, not a mode of `app`/`worker`/`beat`). Not a
Celery task, and not folded into the existing `worker` container either.

**Why not a Celery task, or a task run *by* the existing worker:** Celery's model is
competing consumers pulling discrete task messages off RabbitMQ — no concept of a
partition or a consumer-group offset. A Kafka consumer group is a structurally
different thing: partitions get assigned to group members, and ordering is only
guaranteed *within* a partition, to whichever single consumer owns it. Running this as
a Celery task (or a background loop bolted onto `worker`) would throw away exactly the
guarantee BOOK-49's `reservation_id` partition key exists for — `worker` is already
scaled independently for Celery task throughput (`--scale worker=N`), and tying Kafka
group membership to that same scaling knob would couple two unrelated decisions
together for no benefit.

**Why not just embed the poll loop in `app/worker.py`'s process:** same reason, one
level down — `worker`'s process model (Celery's own pool/prefork machinery) has no
hook for "also run one independent infinite loop," and doing it anyway would tie this
consumer's health to Celery's, and vice versa, when the two have nothing to do with
each other.

**Loop shape:** `run()`/`_process_one()` (`app/notification/consumer.py`) take the
per-message handler as a parameter rather than importing `notification/service.py`
directly, so the poll → parse → dispatch → commit control flow is unit-testable with a
fake handler. The concrete handler — open a `UnitOfWork`, route by `event_type`, call
`send_reservation_confirmed_email`/`send_reservation_cancelled_email` — is assembled by
the top-level `app/notification_consumer.py` entrypoint, mirroring the "thin wrapper
opens infra, real logic lives in a service module" split `app/worker.py` already uses
for its Celery tasks.

**Commit-after-handling, not auto-commit:** `enable.auto.commit` is off; the offset is
committed only once the handler has actually run to completion. Auto-commit would ack a
message before we know its email actually sent — a crash mid-handling would then lose
that notification silently instead of just risking a harmless duplicate. Combined with
BOOK-50's at-least-once relay, this consumer must already tolerate seeing the same
`BookingConfirmed`/`BookingCancelled` twice. **Revised by §13 below:** unlike BOOK-45/47's
request-received email, a duplicate confirmation/cancellation email is *not* accepted as
low-stakes here — it reads as "did this happen twice?" to a guest. §13 adds real
deduplication instead of just tolerating the repeat.

**One message at a time, no concurrency:** `_process_one()` fully handles (and commits)
one message before the next `poll()`. Processing messages for the same `reservation_id`
concurrently could let a later-in-topic-order event's email go out before an
earlier one's, quietly defeating the reason a partition key exists in the first place.

**Revisit when:** throughput on this topic becomes real enough that one message at a
time is a bottleneck — at that point, a bounded thread/process pool *per partition*
(not just "more consumers," which Kafka would rebalance across partitions anyway) is
the next lever, not a redesign of this loop.

---

## 13. BOOK-51: real deduplication for the confirmation/cancellation emails — the Inbox pattern

**General pattern, no BookIt specifics: `inbox-basics.md`.** This section is the
project-specific decision; that doc covers why at-least-once processing needs it at
all, and why a real email send (§3's "Case B" there) can narrow the duplicate window
but never fully close it.

**Why §12's "duplicates are fine" doesn't hold here, on reflection.** That reasoning was
copied from BOOK-45/47's request-received email, but the two aren't the same shape. A
missed-or-duplicated "we got your request" email is invisible either way — the guest's
`201` already told them it worked. A **second** `BookingConfirmed`/`BookingCancelled`
email is visible and asks a question the guest shouldn't have to ask: *did something
happen twice?* That's a real support-ticket risk, not a hypothetical — worth spending
real deduplication on, not just tolerating.

**The pattern: Inbox, the consumer-side mirror of Outbox.** Before acting on a message,
check "have I already processed this exact event?" via a durable, atomic check — don't
trust the broker to never redeliver (BOOK-50's relay is explicitly at-least-once,
outbox-basics.md §5). Concretely, a `processed_notification_events` table:

```
processed_notification_events
-------------------------------
event_id   UUID, primary key
processed_at   timestamptz
```

Same shape of problem as `IdempotencyKey` (`app/reservation/models.py`, BOOK-36), but
**not** the same solution — `IdempotencyKey` inserts first and lets a PK constraint
reject a repeat atomically, because it has to defend against a real concurrent race
(two overlapping HTTP requests racing to create a reservation). This consumer doesn't
have that race: Kafka guarantees exactly one consumer in group `notifications` ever
owns a given partition at a time, so there is never a second, concurrent execution of
`dispatch()` for the same message — only *sequential* redelivery after a crash. A plain
check-then-act (SELECT, then act, then INSERT) is safe under that condition, so
`make_dispatcher()` (`app/notification/consumer.py`) uses one instead of reaching for
the insert-first trick `IdempotencyKey` needs for an actually-concurrent caller.

**The implemented flow, per message (`make_dispatcher()`'s `dispatch()`):**

```
Kafka message
     │
     ▼
payload["event_id"]
     │
     ▼
uow.session.get(ProcessedNotificationEvent, event_id)
     │
     ▼
already exists? ──yes──▶ return (skip -- already handled, this is a redelivery)
     │
     no
     ▼
handler(uow, reservation_id)   # sends the actual email
     │
     ▼
email send returned without raising
     │
     ▼
uow.session.add(ProcessedNotificationEvent(event_id=event_id))
     │
     ▼
UnitOfWork commits (both the fact "processed" and anything the handler wrote)
```

If `handler()` raises, nothing after it runs — no marker row, no commit, the
`UnitOfWork` rolls back, and the exception propagates out of `_process_one()`, which
never calls `consumer.commit(msg)`. The message is redelivered on restart and the whole
`dispatch()` runs again from the top, exactly the retry this design accepts.

**Dedup key: the outbox event's own `id`, not `(reservation_id, event_type)`.** The
composite would technically work today — the state machine only ever fires
`BookingConfirmed`/`BookingCancelled` once per reservation — but it's borrowing that
invariant to do a job it doesn't actually describe. `event_id` answers "which specific
event is this," `reservation_id` answers "which reservation," and conflating them means
silently assuming one `BookingConfirmed` per reservation forever. `OutboxEvent.id`
(already a UUID PK column, `app/core/outbox/models.py`) already *is* the right key; it
was just never included in the JSON payload BOOK-49 writes. **Touched BOOK-49 for this:**
`_emit_outbox_event()` (`app/reservation/service.py`) now generates the id explicitly
(`event_id = uuid.uuid4()`), passed as both `OutboxEvent(id=event_id, ...)` and
`payload["event_id"]`, rather than relying on the column's ORM-level default — the
payload has to carry the same value that ends up in the row, and a default evaluated at
flush time doesn't guarantee that without being read back afterward. `event_id` is now
in every event's payload, alongside the existing `event_type`/`reservation_id`/
`occurred_at`.

**Mark *after* sending, not before — the same failure-mode trade BOOK-50's relay already
made, applied one layer downstream:**

| Order | Fails how |
|---|---|
| Mark processed, then send | Crash between the two → event is marked done, email never sent — a **silent loss**, worse than a duplicate |
| Send, then mark processed | Crash between the two → email sent again next redelivery — a **duplicate**, the thing §13 exists to reduce, not the thing it must reach zero |

BOOK-50's relay already made this exact call (`relay.py`: `published_at` is set only
for rows Kafka actually confirmed, never optimistically beforehand) — marking-after
trades a narrow duplicate risk for ruling out silent loss, and that trade is right again
here for the same reason: a guest who never got their confirmation email is a worse
outcome than a guest who got it twice.

**The actual target, stated precisely — not exactly-once, because plain SMTP can't give
that:** at-least-once event processing, paired with a consumer that makes duplicates
*rare* (the send→mark window is short — one SMTP call — not eliminated. A crash inside
that exact window still double-sends.) Nothing in this stack (confluent_kafka, plain
SMTP/MailHog) offers a transactional outbox↔inbox handshake that would close that last
window; closing it fully would need the email provider itself to support an idempotency
key on the send call (Postmark/SendGrid do; MailHog doesn't).

**Revisit when:** a real email provider replaces MailHog — if it supports send-level
idempotency keys, `event_id` doubles as that key and the dedup responsibility can move
to the provider, making even the narrow send→mark window safe.

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
| BOOK-47 task-body retry | `autoretry_for` on connection-level errors, backoff+jitter, max_retries=5 — verified live | Never — this is the settled policy for this task |
| BOOK-47 publish-call retry | Deliberately none — would block the guest's request | Never, for this job — see §9/§10's "retry that blocks" reasoning |
| BOOK-47 dead-lettering | `task_failure` signal → manual publish to `notifications.dlq` — RabbitMQ's queue-level DLX doesn't fire here (Celery ACKs failed tasks) | A second job needs the same handling — generalize the signal handler beyond one queue name |
| BOOK-50 relay | Polling (Celery beat, same shape as BOOK-46) — not CDC | A several-second delivery delay genuinely matters for some published event |
| BOOK-51 consumer process | Standalone `confluent_kafka.Consumer`, own docker-compose service — not a Celery task, not folded into `worker` | Never for the process split; revisit one-at-a-time processing if this topic's throughput becomes a real bottleneck |
| BOOK-51 duplicate emails | Real dedup (Inbox pattern), not tolerated like BOOK-45/47 — keyed on `OutboxEvent.id`, marked processed *after* sending | A real email provider adds send-level idempotency keys — dedup responsibility can move there |
