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

## 2. arq, not Celery — *provisional*

**Chosen:** arq.

- **Async-native.** The app is async end to end (FastAPI, async SQLAlchemy). Celery is
  sync-first and needs a bridge to call async code; arq doesn't.
- **Scheduling is built into the worker.** Celery needs a separate `celery beat`
  process purely to enqueue timed jobs — two process types to deploy and watch instead
  of one, and beat does not deduplicate itself, so "run exactly one" becomes a rule you
  have to remember. arq's cron runs inside the worker.
- **Smaller surface** to read and understand, which matters more here than feature
  breadth.

**What Celery is genuinely better at,** so this isn't a one-sided comparison: a much
larger ecosystem, far more production mileage, richer routing and monitoring, and
broker flexibility. If this project ever needs workflow primitives (chains, chords,
groups) or a mature monitoring story, Celery is the stronger choice.

**Revisit if:** we need multi-step workflow orchestration, or arq's smaller community
becomes a problem when debugging something obscure.

## 3. Redis for the task broker — and it need not match the event bus

**Chosen:** Redis, which arq requires anyway. Redis is already running for caching and
rate limiting.

**Correcting an earlier assumption:** it seemed natural to pick RabbitMQ here because
it's already in `docker-compose.yml` and the event-driven work will use it. That
reasoning conflated two different things:

- **A task queue** — "run *this function* later." The library owns the queue; there is
  no topology to design.
- **An event bus** — "*this happened*." Exchanges, routing keys, bindings, several
  independent subscribers per event. The topology *is* the work.

An event bus will need RabbitMQ whether or not tasks run on Redis. They don't have to
share a broker, and using each for what it suits is a normal arrangement rather than a
smell. What the event work genuinely reuses from here is the **worker process** — event
consumers need something long-running outside HTTP to listen in — not the queue.

**Implementation note (provisional):** point arq at a *different logical Redis database*
from the cache (`redis://localhost:6379/1`). Cache keys are disposable and jobs are not;
keeping them apart means a cache flush can never take pending jobs with it, and makes
`redis-cli` inspection far easier to reason about.

**Revisit if:** we ever want a single broker for operational simplicity, or need routing
features Redis can't express.

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

- **How does a job get a database session?** `get_db()` (`core/db.py`) is a
  request-scoped FastAPI dependency and there is no request in a worker. Needs an
  answer before any real job is written.
- **Confirm arq's cron deduplication.** It should derive a deterministic job id per
  firing so only one worker's attempt is accepted, which is what makes scheduled jobs
  safe under multiple instances. Verify rather than assume — it's the difference
  between one expiry run and N.
- **Does the worker ship in the same image as the app?** Same `Dockerfile` with a
  different command is the simpler default.

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| Where work runs | A worker process on a queue | Triggered work and retries both get dropped |
| Library | arq (provisional) | Multi-step workflows, or community size bites |
| Task broker | Redis, separate logical DB from the cache | Need one broker, or routing Redis can't express |
| Task broker vs event bus | Independent; they need not match | — |
| Worker count | One worker, many jobs; scale with copies | Head-of-line blocking, resources, scaling, blast radius, priority |
| First job | The expiry job — idempotent, no external deps | — |
