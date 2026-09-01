# Where should this work run? A decision guide

There are five places code can run, from "inside the request" to "a fleet of workers
on a queue". Each is the *right* answer somewhere. This doc is about recognising
which situation you're in, because the cost of over-building is real and the cost of
under-building shows up as a silent bug months later.

Deliberately general — no BookIt specifics. For which of these this project picked and
why, including what was rejected and when to revisit it, see `worker-decisions.md`.

## 1. Three questions that decide almost everything

Ask them in this order. The first "yes" usually settles it.

1. **Is there a request at all?** If the work happens because *time passed* rather
   than because someone did something, everything request-shaped is out immediately.
2. **Does anyone wait for the result?** If a user is staring at a spinner until it
   finishes, it belongs in the request. If not, it shouldn't be.
3. **What happens if it's silently lost?** "Nothing much" and "a customer doesn't get
   something they paid for" lead to very different answers. This is the question
   people skip, and it's the one that determines whether you need durability.

Two follow-ups that decide the last step up: **does it need to be retried?** and
**will more than one copy of this run?**

## 2. The ladder

### Inline, in the request handler

**Right when:** it's fast (tens of milliseconds), the caller needs the result, and if
it fails the request *should* fail. Writing the row you just validated. Reading what
you're about to return.

**Stop when:** it's slow enough to notice, or when failing it shouldn't fail the
request. The tell is a sentence like *"the booking worked, only the email didn't"* —
if that sentence makes sense, the work doesn't belong inline.

### After the response, same process (`BackgroundTasks`)

Runs once the response is sent, in the same process. Zero infrastructure — that is
its entire appeal.

**Right when:** it's triggered by a request, it's quick, and losing it occasionally
is genuinely fine. An analytics ping, a cache warm, a best-effort audit line.

**Stop when:** losing it matters. A restart mid-task destroys it with no record it
was meant to happen. There is no retry, no visibility, and no way to know it didn't
finish. Also useless for anything not triggered by a request.

### A loop inside the app process

An `asyncio` task started at boot that sleeps and repeats.

**Right when:** the work is periodic, **idempotent**, low-stakes, and you run exactly
one instance. If missing a run just means the next run picks it up, this is honestly
fine, and it costs nothing.

**Stop when:** you run more than one instance (all of them fire it — §4), or the work
needs to be retried on failure, or it must survive a restart mid-flight.

**If you do this, wrap each iteration in `try/except`.** One uncaught exception kills
the loop permanently while the app keeps serving traffic and looking perfectly
healthy — which is about the worst failure mode available, because nothing tells you.

### OS-level scheduling (cron, systemd timers, Task Scheduler)

A scheduler outside your app invokes a script.

**Right when:** the work is periodic, the machine is long-lived and known, and the job
is a self-contained script. This is genuinely the right tool for ops work — backups,
log rotation, database maintenance, certificate renewal — and has been for decades.
Don't let "we have a worker" push you into running a nightly `pg_dump` through it.

**Stop when:**
- **the work is triggered rather than timed.** "Do this because a booking was
  created" has no cron shape at all.
- **it needs retry.** cron fires and forgets; a failed run just didn't happen.
- **instances are interchangeable.** "Install it on only one machine" means the thing
  keeping you correct is a fact about one box that no code records. Rebuild it and the
  job silently stops; clone it and everything runs twice.
- **local and production differ.** A `.bat` on a dev laptop is not what a Linux
  container will run, so you'd be testing a different mechanism than you deploy.

### A worker on a queue

A separate process that pulls jobs and runs them.

**Right when any one of these is true:**
- work is *triggered* and must not be lost
- it needs retry with backoff, and somewhere for jobs that exhaust their attempts
- you have **both** scheduled and triggered work — otherwise you maintain two
  mechanisms
- it must keep running when the web process restarts
- you'll run more than one instance

**The cost is real:** a broker, a second process type to deploy and watch, and a new
question — the worker has no HTTP request, so it needs its own database session
rather than a request-scoped dependency.

**The pattern to watch for:** each of a loop's limits is patchable — add a lock for
duplicates, a table for retry state, backoff, a dead-letter store. Each patch is
reasonable alone. Four of them mean you've hand-built a worker library with fewer
features. Notice it at patch two, not patch four.

## 3. Inside one worker with several jobs

Having picked a worker, it's worth knowing what it actually does at runtime — most of
the early surprises live here, before any question of scaling.

**It runs several jobs at once, not one at a time.** A worker has a concurrency limit
(arq's `max_jobs`, default 10) and fills it. While one job waits on I/O, the others
run:

```
worker (one process)
├── send_email(res-1)      ← awaiting SMTP
├── send_email(res-2)      ← awaiting SMTP
└── expire_reservations()  ← awaiting the database
```

**But "at once" means interleaved, not simultaneous.** There is one thread, and at any
given instant exactly one job is executing Python. Ten jobs are all *in progress*; they
take turns:

```
time →
job A  ███░░░░░░░░░░░░░███         ███ = running Python
job B     ███░░░░░░░░░░░░░███      ░░░ = awaiting I/O, using no CPU
job C        ███░░░░░░░░░░░░░
       ↑
       any vertical slice: exactly one ███
```

Every `await` — a database query, an SMTP call — is a job saying *"I'm waiting, give
someone else a turn."* The event loop hands control to the next one.

**Why that helps at all:** these jobs spend nearly all their time waiting rather than
computing. Ten emails each waiting one second on SMTP take ten seconds sequentially and
about one second interleaved. Nothing ran faster — the idle waiting simply stopped
being wasted.

**And that is exactly why a blocking call is so destructive.** `time.sleep(5)` or a
synchronous `requests.get()` never yields; it holds the thread and no one else gets a
turn:

```
job A  ██████████████████    ← blocks for 5s
job B                        ← does not run. at all.
job C                        ← does not run. at all.
```

`await asyncio.sleep(5)` yields, and B and C carry on. So inside a job: `await`
everything, async libraries only. Genuinely CPU-bound work needs a thread or process
pool, never the event loop.

**Real parallelism comes from more processes.** Three worker containers means three
event loops on three cores, genuinely simultaneous. That's what scaling out buys, and
it's a different axis from `max_jobs`:

| | Concurrency (`max_jobs`) | Parallelism (more workers) |
|---|---|---|
| What it is | Many jobs in progress, taking turns | Many jobs truly running at once |
| Needs | One event loop | Several processes / cores |
| Helps with | Waiting on I/O | Heavy computation |

**Overflow just waits, which is the point.** Fifty jobs arrive at once with
`max_jobs=10`: ten run, forty sit in the queue. Nothing is dropped and nothing is
overwhelmed — the queue is a buffer, and back-pressure comes free. An in-process loop
gives you no such protection; work either happens now or is lost.

**One job failing doesn't touch the others.** The library catches the exception, marks
that job failed, and moves to the next. The worker keeps running.

This is the substantive difference from a hand-rolled loop, where a single uncaught
exception kills the mechanism permanently while the app carries on looking healthy.

**Scheduled jobs can overlap themselves.** If a job runs every minute and one run takes
ninety seconds, the next firing starts while the previous is still going — two
executions over the same rows.

For idempotent cleanup that's harmless. For anything else it's a real bug, and the fix
is a lock or an "already running" flag. Worth deciding per scheduled job rather than
discovering it under load.

Jobs also finish in an unpredictable order — see §4.

## 4. What changes when you run more than one

The single most important distinction here, and it isn't about your app — it's about
which *kind* of work you have.

### Triggered work scales for free

Several consumers on one queue is the **competing consumers** pattern. The broker
hands each message to exactly one of them:

```
                    ┌── worker A   (takes job 1)
enqueue ─→ [queue] ─┼── worker B   (takes job 2)
                    └── worker C   (takes job 3)
```

Nothing is configured to make that happen — it's what a queue *is*. Three workers is
roughly three times the throughput with no code change.

### Scheduled work does not

A cron-style job has no enqueuer. Every instance's own scheduler notices "it's 14:00"
simultaneously, so without coordination **all of them fire it**:

```
14:00 ─→ instance A fires expire_bookings
      ─→ instance B fires expire_bookings
      ─→ instance C fires expire_bookings
```

How bad that is depends entirely on the job. Idempotent cleanup: mostly noise. Sending
email: every customer gets three.

**Three ways out, and it's worth knowing which one you're relying on:**

| | |
|---|---|
| The library handles it | arq derives a deterministic job id per cron firing; ids are unique in Redis, so only one attempt is accepted. Verify this rather than assuming it |
| A separate scheduler process | Celery's `beat` enqueues and workers execute — but it does **not** deduplicate, so the rule is *exactly one* beat |
| A distributed lock | The legitimate use in `docs/cache/distributed-lock-basics.md` — losing it costs duplicated work, never corrupted data |

> Triggered work is safe under N instances for free. Scheduled work needs someone to
> decide who fires it — and if your tool doesn't, you do.

### Two things that come with any queue

**Delivery is at-least-once, so jobs must be idempotent.** A worker that crashes after
doing the work but before acknowledging leaves the message un-acked, and the broker
gives it to somebody else. That redelivery is why a crash doesn't lose work — but it
means every job must be safe to run twice. Same reasoning as
`docs/db/idempotency-basics.md`, and sharper with retries: a job that *timed out* may
in fact have succeeded.

**Ordering is not guaranteed.** Two jobs enqueued in order can finish out of order on
different workers. Never write a job assuming an earlier one finished — pass what it
needs, or re-read current state.

## 5. When one worker becomes several

One worker process handles many different job functions; you scale by running more
*copies of the same worker*, not by building a worker per task. Split into separate
workers or queues only when a specific pressure appears:

| Reason | What it looks like |
|---|---|
| **Head-of-line blocking** | A ten-minute report occupies workers while one-second emails queue behind it |
| **Different resource profiles** | Image processing wants memory, email wants none; sizing one pool for both wastes the larger number |
| **Different scaling curves** | Ten workers for email, one for reports — impossible sharing a pool |
| **Blast radius** | A job that can hard-crash its process shouldn't take unrelated jobs with it |
| **Priority** | A user-visible job shouldn't wait behind a nightly batch |

Every row is the same shape: *one queue forces a single answer where two different
answers are needed*. Until two jobs genuinely disagree about timing, resources, or
scaling, splitting buys operational surface for nothing.

---

## Cheat sheet

| Option | Right when | Falls over when |
|---|---|---|
| **Inline** | Fast, caller needs it, failure should fail the request | "It worked, only the X didn't" makes sense |
| **`BackgroundTasks`** | Request-triggered, quick, losing it is fine | Losing it isn't fine; no retry; no request exists |
| **In-process loop** | Periodic, idempotent, low-stakes, one instance | 2+ instances; needs retry; must survive restart |
| **OS cron** | Periodic, fixed machine, standalone script, ops work | Triggered work; needs retry; interchangeable instances |
| **Worker + queue** | Triggered *and* durable, retry, both work kinds, N instances | Cost isn't justified — one loop would have done |
| **Several workers** | Head-of-line blocking, resources, scaling, blast radius | Nothing is actually competing yet |

| Term | Meaning |
|---|---|
| Triggered (deferred) job | "Run this later." Something enqueues it |
| Scheduled (cron) job | "Run this because time passed." Nobody enqueues it |
| Competing consumers | N workers on one queue; each message goes to exactly one. Free scaling |
| The N-instances trap | Triggered work is safe under N workers; **scheduled work is not** |
| At-least-once | A crash before ack means redelivery — so every job must be idempotent |
| Worker ≠ per task | One process, many job functions. Scale with more copies of the same worker |
