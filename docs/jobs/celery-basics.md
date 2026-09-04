# Celery, in the amount of depth BOOK-44 actually needs

Deliberately general — no BookIt specifics until the last section. Companion to
`rabbitmq-basics.md` (the broker underneath) and `background-jobs-basics.md` (when a
worker is the right tool at all). For what this project chose and why, see
`worker-decisions.md`.

## 1. What Celery actually is

RabbitMQ moves messages; it has no idea what a "task" is, what Python function to run,
or how to serialize an argument. Celery is the layer above the broker that turns
"a message arrived" into "call `send_email(booking_id=42)`":

```
your code ──▶ Celery app ──▶ broker (RabbitMQ) ──▶ Celery worker ──▶ your function
             (client side,                        (consumer side,
              publishes)                           executes)
```

The same **Celery app object** is used on both sides — it's what makes `task.delay()`
on the producer side and `@app.task` on the consumer side agree on how to serialize
arguments, which queue to use, and where the broker is.

## 2. The app, the task, and the two ways to enqueue

- **The app** (`Celery(...)`) is the central config point: broker URL, optional result
  backend, serialization format, default queue/exchange (`rabbitmq-basics.md` §8).
- **`@app.task`** turns an ordinary function into something callable two ways: directly
  (runs in-process, no broker involved — useful in tests) or via `.delay(...)` /
  `.apply_async(...)` (publishes a message, returns immediately).
- **`.delay(*args, **kwargs)`** is shorthand for the common case.
- **`.apply_async(...)`** is the full form — same thing, plus options `.delay()` can't
  express: `countdown=30` (run in 30s), `eta=<datetime>` (run at a specific time),
  a specific queue, a specific `task_id`, retry policy overrides.

Calling either **does not run the function**. It serializes the arguments, publishes a
message to the broker, and returns a handle (`AsyncResult`) immediately. The function
body only runs inside a worker process, later, possibly on a different machine.

## 3. What the worker process actually is

`celery -A yourapp worker` starts a process that does three things: connect to the
broker, pull messages off the queue(s) it's configured to consume, and dispatch each
one to the matching `@app.task`-decorated function by name.

**Concurrency model — this is a real choice, not a detail:**

| Pool | Model | Right for |
|---|---|---|
| **prefork** (default) | Several OS processes, real parallelism | CPU-bound work, or blocking/sync code — each process blocks independently, others keep running |
| **eventlet / gevent** | Greenlets in one process, cooperative | Very high-concurrency I/O-bound work, but *only* if every library involved is patched/async-aware |
| **solo** | Single thread, no concurrency at all | Debugging only |

Prefork is the default for a reason: it tolerates ordinary blocking/synchronous code
without needing every library in the call path to cooperate with an event loop. If a
task calls `asyncio.run(...)` internally to reach async code (as this project's DB
layer will require — see `worker-decisions.md` §2's open question), prefork is the
straightforward fit: each worker process gets its own event loop per task, with no
event-loop-sharing complexity between concurrent tasks the way eventlet/gevent would
introduce.

## 4. Task states, and what they require

A task's lifecycle — `PENDING → STARTED → SUCCESS`/`FAILURE`/`RETRY` — is only
*observable* if something stores it. That something is the **result backend**
(commonly Redis or a database table), and it's entirely separate from the broker:

| | Job | Needed for |
|---|---|---|
| **Broker** (RabbitMQ) | Gets the task *to* a worker | Always required |
| **Result backend** (optional) | Stores what happened *after* | `.get()`, checking status, retrieving a return value |

No result backend configured means genuinely fire-and-forget: the task still runs, but
nothing records whether it succeeded, and calling `.get()` on the returned
`AsyncResult` has nothing to fetch. That's a fine starting point — add a backend only
when a real feature needs to ask "did it work?"

## 5. Retries — mentioned here, built in BOOK-47

Celery supports both a declarative form (`autoretry_for=(SomeError,)`,
`retry_backoff=True`, `max_retries=5` on the task decorator) and a manual one
(`self.retry(...)` called from inside the task body when you need to inspect the
failure first). Either way, this is **application-level** retry — a business-logic
decision about a task that was delivered and ran, but failed — as distinct from the
broker redelivering an un-acked message after a crash (`rabbitmq-basics.md` §4). Both
matter; they're not the same mechanism and don't compete. This project's actual retry
policy is BOOK-47's job, not something to build ahead of it.

## 6. Serialization, and the argument-shape trap

Task arguments are serialized (JSON by default) to cross the wire to the worker. This
means:
- Arguments must be JSON-serializable — a `UUID` or `datetime` needs to be passed as a
  string and reconstructed inside the task, not passed as the Python object directly.
- **Pass IDs, not objects.** Never pass a loaded ORM instance or a live DB session as a
  task argument — it won't serialize, and even if it somehow did, it would be stale by
  the time the worker (a different process, possibly a different machine) picks it up.
  Pass `booking_id`; have the task load fresh state itself.

## 7. Idempotency and task identity

Every `.delay()`/`.apply_async()` call gets a fresh, random `task_id` by default — so
calling it twice with identical arguments produces two independent tasks, not one
deduplicated task. If "don't enqueue this twice" ever matters (it doesn't for BOOK-44's
trivial task), that has to be built deliberately — e.g. an explicit, deterministic
`task_id` derived from the business key, or a lock — Celery does not infer intent from
arguments. This is separate from, and in addition to, the at-least-once/idempotent-body
requirement in `rabbitmq-basics.md` §4: that one is about a task *running twice*, this
one is about a task being *enqueued twice*.

## 8. Beat — a scheduler, not a worker

`celery beat` is a separate, lightweight process. All it does is enqueue tasks on a
schedule — it never executes anything itself; an ordinary worker still has to pick up
what beat enqueues. This matters for BOOK-46 (the scheduled expiry job), not BOOK-44,
and it comes with a sharp edge already recorded in `worker-decisions.md` §2 and
`background-jobs-basics.md` §4: beat does **not** deduplicate itself, so running more
than one instance means the same scheduled task fires multiple times. "Exactly one
beat" is a rule you enforce, not a guarantee Celery gives you.

---

## How this maps onto BOOK-44, specifically

The smallest correct slice: one `Celery` app pointed at RabbitMQ, `task_acks_late =
True` set explicitly (`rabbitmq-basics.md` §4 — this is the setting that makes the
crash-recovery story real instead of theoretical), one trivial `@app.task` with no DB
and no external calls, a `prefork`-pool worker, and no result backend. Everything else
in this doc — retries, beat, custom serialization, dedup — belongs to a later ticket,
not this one.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Celery app | Central config: broker URL, result backend, serialization, defaults |
| `@app.task` | Turns a function into something callable directly *or* via the broker |
| `.delay()` | Shorthand enqueue — args/kwargs only |
| `.apply_async()` | Full enqueue — countdown, eta, queue, task_id, retry overrides |
| Broker vs. result backend | Broker = gets the task there (required). Backend = records what happened (optional) |
| Prefork | Default worker pool — real OS processes, tolerates blocking/sync code |
| Task retry vs. broker redelivery | Retry = app-level, for a task that ran and failed. Redelivery = broker-level, for a crash before ack |
| `task_id` | Not deduplicated by default — two identical calls make two tasks |
| Beat | Enqueues on a schedule; never executes; must run as exactly one instance |
