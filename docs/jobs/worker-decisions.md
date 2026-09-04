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

## 6. Open questions for `BOOK-44`

- **How does a Celery task get a database session, given the app is async?**
  `get_db()` (`core/db.py`) is a request-scoped, async FastAPI dependency and Celery
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
