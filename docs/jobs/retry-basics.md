# Retry, in the amount of depth BOOK-47 needs

Deliberately general — no BookIt specifics. For what this project chose (which
failures retry, backoff numbers, where jobs go when they exhaust retries) and why, see
`worker-decisions.md`.

## 0. The mental model, if you remember nothing else

```
          Retry Policy
               │
     ┌─────────┼─────────┐
     ↓         ↓         ↓
  Which?    How long?   How many?
 transient   backoff     max retries
     │         │         │
     └─────────┼─────────┘
               ↓
             failed
               ↓
              DLQ

+ Idempotency
+ distinguish job retry from enqueue retry
```

In plain words: a retry policy says *what* gets tried again, *when* it gets tried again,
*how many times*, and *what happens if it still fails* — all while assuming the
underlying action is safe to run more than once. Sections 1–4 below are those four
questions, one at a time; sections 5–6 are the two traps that don't show up in the
diagram but break the policy anyway if missed — retrying the wrong *thing* (§5), and
retrying somewhere that costs more than it should (§6).

## 1. The core distinction: transient vs permanent failure

Not every failure deserves another attempt. The one question that decides it:

**Would trying again, unchanged, have any chance of succeeding?**

- **Transient** — the failure is about *timing*, not the request itself. The mail
  server was briefly overloaded; the network blipped; a lock was held by someone else
  for a moment. Try again in a bit and it plausibly works.
- **Permanent** — the failure is about the request itself, and trying again with the
  exact same input produces the exact same failure, forever. A malformed payload, a
  recipient address that doesn't exist, a row that was deleted.

Retrying a permanent failure doesn't add resilience — it just burns time and queue
capacity re-discovering the same broken thing, repeatedly, while looking like progress
is being made.

**Getting this right means classifying failures deliberately, not catching everything.**
"Catch `Exception`, retry" is the easy version and the wrong one — it retries typos and
deleted records exactly as eagerly as it retries a genuinely flaky connection. The real
work of a retry policy is building the list of *which specific errors* are worth another
attempt, and treating everything else as final.

## 2. Backoff — why waiting longer each time, not the same amount

Retrying immediately after a failure is often the worst time to retry — if the failure
was "the server is overloaded," hitting it again a millisecond later adds to the load
that caused the problem in the first place. Backoff means waiting between attempts, and
waiting *longer* each time:

```
attempt 1 fails → wait 1s
attempt 2 fails → wait 2s
attempt 3 fails → wait 4s
attempt 4 fails → wait 8s
...
```

This is **exponential backoff** — each wait roughly doubles the last. It gives a
struggling system increasing room to recover instead of a steady drumbeat of retries
holding it down.

**Jitter — a small random amount added to each wait — solves a different problem:**
without it, if a hundred callers all failed at the same moment (a server restarted,
say), their retries stay synchronized and hit the recovering server in the same tight
bursts, attempt after attempt. Jitter spreads them out so recovery isn't immediately
re-trampled by the exact crowd that just failed together.

**A ceiling matters too.** Exponential growth unchecked eventually means waiting hours
between attempts for something that should resolve in minutes — a `max backoff` caps how
long any single wait gets, no matter how many attempts have already failed.

## 3. Giving up — retry forever is not resilience, it's a hidden outage

A job that retries without limit, on a failure that will never resolve, looks alive
(something is happening!) while doing nothing useful, forever. That's worse than
failing loudly: a request that fails immediately gets noticed; a job silently retrying
in the background for three days does not, until someone asks why an email from three
days ago never arrived.

**A max-attempts count is what turns "retry" from a hope into a bounded decision.** Once
exhausted, the job needs somewhere to go that isn't "vanish":

**A dead-letter queue (DLQ)** is that somewhere — a separate destination for jobs that
failed every attempt, held for inspection rather than discarded. It turns "silently
lost forever" into "visibly stuck, in one place, for a human or a follow-up process to
look at." Whether a broker gives you this for free or you build it yourself depends on
the broker — worth checking rather than assuming (`rabbitmq-basics.md` §6 and
`background-jobs-basics.md` §4 cover this for the brokers this project actually uses).

## 4. Idempotency — the assumption retry quietly depends on

At-least-once delivery (any real queue's guarantee — `rabbitmq-basics.md` §4) plus
retry together mean **a job's side effect might happen more than once**, even for a
single logical attempt: a job can time out *after* it actually succeeded, so a retry
reruns work that already landed.

A retry policy is only safe on a job that's **idempotent** — running it twice produces
the same end state as running it once, or at worst a harmless duplicate. Retrying a
non-idempotent job (charging a card twice, say) doesn't add reliability, it adds a new
bug. This is the same requirement `docs/db/idempotency-basics.md` covers for the
request/response side of the system — retry is where it matters again, on the
job-processing side.

## 5. Two different things people mean by "retry" — this is the trap

**Retrying a job that already started** — it was accepted, began running, and failed
partway (or failed to complete) *doing its work*. This is what a task framework's
built-in retry (`autoretry_for`, `retry_backoff`) is for: the job exists, is tracked,
and the framework re-runs it.

**Retrying the act of starting a job at all** — the *attempt to enqueue it* failed,
because whatever accepts jobs (the broker, the queue, the API) wasn't reachable in the
first place. No job was ever created, so there's nothing for a task-level retry
decorator to re-run — that mechanism only ever sees jobs that successfully got in the
door.

These need genuinely different handling. A retry decorator on the job function solves
the first case completely and does *nothing* for the second — the failure it would need
to catch happens one layer earlier, at the call that tries to hand the job to the queue
in the first place.

**A related pair that looks identical from the outside but comes from opposite
directions: your own retry vs. the broker's redelivery.** Both end with the worker
receiving the job again — but *who decided that*, and *why*, are completely different:

```
Your retry (application decides)          Broker redelivery (broker decides)

Worker receives the job                   Worker receives the job
        │                                         │
   job fails                                 💥 worker dies before acking
        │                                         │
   framework says: retry                     broker sees no ack
        │                                         │
   a NEW attempt is sent to the broker        broker resends the SAME message
        │                                         │
        ▼                                         ▼
   worker receives it again                  worker receives it again
```

The outcome looks the same (the worker gets the job again) — but the cause is opposite:
**your retry means the framework explicitly decided the work needs another attempt**
(it ran, it failed, try again); **redelivery means the broker never got confirmation the
first attempt happened at all** (no verdict on the work — just a missing acknowledgment).
This is exactly why *your* retry can have backoff, jitter, and a max-attempts count —
the framework is choosing to send a new attempt and can pace it however it wants.
Redelivery has none of that: the broker isn't evaluating whether the work succeeded, it's
reacting to silence, and it hands the same message back with no concept of "wait a bit
first." (This project's specific instance: `rabbitmq-basics.md` §4's ack-late mechanism
is the redelivery side; Celery's `autoretry_for` is the retry side — same-looking
outcome, opposite triggers.)

## 6. Retry that blocks vs. retry that doesn't — where you retry changes what it costs

Not all retry loops cost the same. The deciding question: **who is waiting while the
retry happens?**

- **Retry inside a background worker, off any request path** — free real estate.
  Nobody is blocked; the job takes a little longer to finish, unnoticed.
- **Retry inside something synchronous — a request handler, a call the caller is
  waiting on** — every attempt and every backoff wait adds directly to how long the
  caller sits there. A retry loop that's perfectly reasonable in a worker becomes a
  user-facing hang if it runs inline in a request.

**This is worth checking explicitly, not assumed:** the same failure (a broker
unreachable, say) can be one call away from either situation, depending on *where* the
enqueue attempt lives in the code — and that's exactly the distinction from §5 that
matters here, since "retrying the enqueue" often means retrying from inside whatever
just tried to create the job, which may or may not be a request handler.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Transient vs. permanent failure | Would retrying, unchanged, plausibly succeed? Only transient failures are worth retrying |
| Exponential backoff | Each wait roughly doubles the last, giving a struggling system room to recover |
| Jitter | Randomizes each wait so synchronized retries don't re-trample recovery in lockstep |
| Max backoff | A ceiling on how long any single wait grows to |
| Max retries | A bound that turns "retry" into a decision, not an unbounded hidden outage |
| Dead-letter queue (DLQ) | Where a job goes after exhausting retries — visible and inspectable, not silently gone |
| Idempotency (for retry) | Running the job twice must be harmless — at-least-once delivery means it might |
| Retrying a running job vs. retrying to start one | Two different failures, two different fixes — a task-level retry decorator only covers the first |
| Your retry vs. broker redelivery | Same-looking outcome (job runs again), opposite cause: you decided it needs another attempt vs. the broker never got an ack |
| Retry that blocks | Retrying inside a synchronous call path costs the caller directly — check where the retry actually lives |
