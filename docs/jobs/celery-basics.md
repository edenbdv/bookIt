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

**"Same app object" means the same code, not the same object in memory.** The API and
the worker are separate OS processes — the API doesn't hold a live reference into the
worker's memory, or vice versa. What actually happens is both processes independently
`import app` from the same module, each getting their own in-process `Celery` instance,
constructed from identical config:

```
              celery_app.py
              (one definition)
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
   API process              Worker process
   imports `app`            imports `app`
        │                       │
   its own Celery            its own Celery
   instance, in its          instance, in its
   own memory                own memory
        │                       │
   task.delay(...) ──▶ RabbitMQ ──▶ @app.task-registered function
```

"Shared" is true at the level of *configuration* — both processes agree on the broker
URL, serialization, `task_acks_late`, and so on, because they read it from the same
source. It is not true at the level of the Python runtime — there are two objects, in
two processes, that happen to be configured identically. RabbitMQ (the actual broker)
is the only thing that's genuinely shared infrastructure between them.

## 2. The app, the task, and the two ways to enqueue

- **The app** (`Celery(...)`) is the central config point: broker URL, optional result
  backend, serialization format, default queue/exchange (`rabbitmq-basics.md` §8).
- **`@app.task`** turns an ordinary function into something callable two ways.

**This dual nature is the entire point of the decorator, so it's worth making fully
explicit rather than a passing remark.** Take:

```python
@app.task
def send_email(booking_id):
    print(f"sending email for {booking_id}")
```

**Called directly, it's just a function** — runs immediately, in whatever process
called it, no broker, no worker, nothing async about it at all:

```
send_email(42)
        │
        ▼
runs right now, in this process
returns "sending email for 42"'s side effect immediately
```

**Called via `.delay(...)`, nothing runs yet** — the call returns immediately, and the
function body executes later, in a different process, only once a worker picks it up:

```
send_email.delay(42)
        │
        ▼
Celery serializes the args, publishes a message
        │
        ▼
     RabbitMQ                 (send_email.delay() has already returned by now)
        │
        ▼
a worker process receives the message
        │
        ▼
send_email(42) actually runs — here, not in the caller
```

Same function, two entirely different execution models, chosen by how you call it:

| Call | Runs | When |
|---|---|---|
| `send_email(42)` | In the caller's own process | Immediately, synchronously — useful for tests, or code that genuinely wants it inline |
| `send_email.delay(42)` | In a worker process | Later, whenever a worker is free — the caller never blocks on it |

This is also exactly why calling the plain function directly is a normal thing to do in
a test (no broker needed to exercise the function's logic), while production code that
wants the "don't block, don't lose it" behavior must remember to call `.delay(...)` —
forgetting the `.delay` and calling the function directly is a real, easy-to-make bug:
it still "works" (the function runs), just synchronously and inline, silently defeating
the entire reason it was made a task.

- **`.delay(*args, **kwargs)`** is shorthand for the common case.
- **`.apply_async(...)`** is the full form — same thing, plus options `.delay()` can't
  express: `countdown=30` (run in 30s), `eta=<datetime>` (run at a specific time),
  a specific queue, a specific `task_id`, retry policy overrides.

**What `@app.task` is actually doing underneath — the task registry.** It isn't just
adding a `.delay()` method for convenience. The moment the decorator runs (at import
time), it registers the function in the app's **task registry**, under a name — by
default the function's dotted import path, e.g. `app.worker.send_email`:

```
@app.task
def send_email(...): ...
        │
        ▼
Celery's task registry (on this app instance)
        │
        ▼
"a task named app.worker.send_email exists, and here's the function for it"
```

That name is the *entire* link between the two sides. `.delay(42)` doesn't send Python
code over the wire — it can't; the worker is a different process, possibly on a
different machine. It sends a message containing the task's **name** and its
(serialized) **arguments**:

```
Producer                     RabbitMQ                   Worker
   │                             │                         │
   │  send_email.delay(42)       │                         │
   │ ──────────────────────────▶ │                         │
   │  {"task": "app.worker.      │                         │
   │   send_email", "args": [42]}│                         │
   │                             │ ───────────────────────▶ │
   │                             │                          │  "I got a message for
   │                             │                          │   app.worker.send_email —
   │                             │                          │   let me check my registry"
   │                             │                          │           │
   │                             │                          │           ▼
   │                             │                          │  found it → call it with (42,)
```

**This is exactly why the producer and the worker must agree on the task's name and
the worker must actually have imported the module that defines it.** If the worker
process never imported `app.worker` (so `@app.task` never ran on its side), its
registry has no entry for `"app.worker.send_email"` — a message arrives asking for a
task the worker doesn't know about, and it errors, even though the *exact same
function* exists, correctly decorated, in the producer's codebase. The registry is
per-process and built by import side effects, not shared automatically just because
both processes run "the same code."

**And this is exactly what's missing without the decorator.** A plain function:

```python
def send_email(booking_id):
    ...
```

can still be called directly (`send_email(42)`, ordinary Python), but it was never
registered anywhere, so it has no `.delay` attribute at all — `send_email.delay(42)`
is an `AttributeError`, not a Celery error, because as far as Python is concerned it's
just a function that happens not to have that method. Nothing was sent to RabbitMQ,
and no worker would have known to run it even if something had been.

The one-sentence version: `@app.task` says *"Celery, this function is a task you can
register, publish through the broker by name, and look up and run inside a worker"* —
without it, there is no task, only a function.

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
what beat enqueues.

**Beat's real role: it's the *producer*, standing in for the API.** Every task needs
something that decides "publish this now" — for a triggered task that's a request
handler calling `.delay()`; for a scheduled task, nothing ever calls `.delay()` from a
request, because there is no request. Beat fills exactly that role on a timer instead:

```
Triggered task:                       Scheduled task:

   API (producer)                        Celery Beat (producer)
        │                                        │
        │ ping.delay()                           │ "it's been a minute —
        ▼                                         │  time to publish
   RabbitMQ                                       │  expire_reservations"
        │                                         ▼
        ▼                                    RabbitMQ
   Worker (consumer)                              │
        │                                         ▼
        ▼                                    Worker (consumer)
     ping()                                       │
                                                   ▼
                                        expire_reservations()
```

Same broker, same worker, same `@app.task` function on the receiving end — the only
thing that changed is *who* decided to publish and *why*:

| Role | Filled by, for a triggered task | Filled by, for a scheduled task |
|---|---|---|
| Producer | The API (a request handler calls `.delay()`) | Celery Beat (a timer firing) |
| Broker | RabbitMQ | RabbitMQ — identical |
| Consumer | A worker | A worker — identical |

So "beat doesn't execute the task" isn't a limitation to work around — it's the same
division of labor a triggered task already has (something decides *when*; a worker
does the actual work), just with the *when* coming from a clock instead of an HTTP
request. The function itself is still written as an ordinary `@app.task`, unaware of
which kind of producer will ever call it.

This matters for BOOK-46 (the scheduled expiry job), not BOOK-44, and it comes with a
sharp edge already recorded in `worker-decisions.md` §2 and `background-jobs-basics.md`
§4: beat does **not** deduplicate itself, so running more than one instance means the
same scheduled task fires multiple times. "Exactly one beat" is a rule you enforce, not
a guarantee Celery gives you.

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
