# RabbitMQ, in the amount of depth BOOK-44 actually needs

Deliberately general — no BookIt specifics, no Celery specifics until the last
section. For what this project chose and why, see `worker-decisions.md`. For Kafka's
different model (used for the event bus instead), see `worker-decisions.md` §3.

## 1. The problem it solves

A producer wants to hand off work without knowing who does it, when, or whether they're
even running right now. RabbitMQ sits in the middle and makes that decoupling durable:
the producer's job ends the moment the broker accepts the message, regardless of
whether a consumer exists yet.

```
producer ──▶ RabbitMQ ──▶ consumer
             (holds the message
              until someone takes it)
```

Compare this to a direct call: if the producer called the consumer directly, both would
need to be up at the same instant, and a slow consumer would slow the producer down.
The broker removes both constraints.

## 2. The building blocks

Four pieces, and the order matters — a message never goes straight into a queue:

```
producer ──▶ exchange ──▶ (binding) ──▶ queue ──▶ consumer
```

- **Exchange** — receives every published message. It has no storage of its own; its
  only job is routing.
- **Binding** — a rule connecting an exchange to a queue, e.g. "route anything with
  routing key `booking.created` to this queue."
- **Queue** — where messages actually sit until a consumer takes them. This is the only
  piece with storage.
- **Consumer** — a process subscribed to a queue.

**The exchange is why "just send it to the queue" is the wrong mental model.** You
never publish to a queue directly; you publish to an exchange with a routing key, and
the exchange's bindings decide which queue(s), if any, receive a copy.

## 3. Exchange types — the actual routing logic

| Type | Behavior | Right when |
|---|---|---|
| **Direct** | Routing key must match a binding key exactly | One message → one specific queue, by exact name |
| **Fanout** | Ignores the routing key; every bound queue gets a copy | Broadcast — every subscriber sees everything |
| **Topic** | Routing key matched against a pattern (`booking.*`, `*.created`) | Subscribers care about a *category* of message, not one exact key |
| **Headers** | Routes on message header values instead of the routing key | Routing logic too complex for a key pattern (rare) |

This is the part of RabbitMQ that has no equivalent in a plain task queue, and no
equivalent in Kafka either — Kafka routes by topic name and partition key, not by
exchange/binding rules. It's also most of what "designing an event bus by hand" (as
opposed to just calling `enqueue()`) actually means.

## 4. Acknowledgements — the mechanism that makes "at-least-once" true

A consumer doesn't just receive a message; it must tell the broker it's done:

- **ack** — "processed successfully, delete it."
- **nack** / **reject** — "failed, do something else with it" (retry, or dead-letter —
  see §6).
- **no response at all** (consumer crashes, connection drops) — the broker treats the
  message as **not delivered** and gives it to another consumer.

```
consumer receives message
        │
        ▼
   [does the work]
        │
   ┌────┴────┐
   ▼         ▼
 crash     finishes
   │         │
   ▼         ▼
message    consumer
redelivered  acks
to someone   │
else         ▼
          message deleted
```

**This redelivery is the entire reliability guarantee, and it depends on *when* the ack
happens.** Two designs are possible, and they produce very different failure modes:

| | When it acks | What a mid-task crash does |
|---|---|---|
| **Ack early** | The instant the message is received, before doing any work | Message is already gone — a crash mid-task **loses the work silently** |
| **Ack late** | Only after the work finishes successfully | A crash mid-task leaves the message un-acked — broker **redelivers it** |

Ack-early is faster and simpler, but it throws away the one thing a message broker is
for. Ack-late is what "RabbitMQ gives you crash recovery" actually refers to — and it
isn't automatic; whatever client library you use has to be configured to do it. Getting
this wrong is invisible until the day a worker actually crashes mid-task, which is what
makes it worth understanding before writing any code, not after.

**These are two separate decisions, made by two separate parties — worth keeping
straight:**

- **When to ack is the worker's decision.** Early or late is a client-side setting
  (in Celery, `task_acks_late`). The broker has no opinion on this; it just waits to be
  told either way, whenever the worker's code/config decides to say so.
- **What happens to a message that never gets acked is the broker's decision, and it's
  not configurable per-message.** If a connection dies with a message outstanding,
  RabbitMQ *always* requeues it for redelivery — that behavior isn't something the
  worker opts into, it's just what the broker does with anything left unacknowledged.

So the worker doesn't decide "redeliver this" — it only decides, by choosing *when* it
acks, how much work is exposed to that redelivery if it crashes. Ack early and the
window is "received, not yet started" — usually empty, so a crash there loses work
silently with no redelivery to save it. Ack late and the window is "the entire task" —
a crash anywhere in it leaves the message unacked, and the broker redelivers
automatically.

**"No ack" alone doesn't trigger redelivery — the broker also has to *notice* the
consumer is gone.** An unacked message isn't sitting back in the visible queue,
waiting; it's parked in a distinct state, tied to the specific connection/channel it
was delivered to:

```
Ready (in the queue, unclaimed)
   │  delivered to a consumer
   ▼
Unacked (claimed by that connection, not yet confirmed)
   │
   ├─ ack arrives ──────────────▶ deleted, done
   │
   └─ broker detects the connection is gone ──▶ moved back to Ready,
                                                  delivered again (redelivery)
```

**Two ways the broker actually detects "the connection is gone":**

- **The TCP connection closes.** A clean crash (the process exits, gets killed) closes
  its sockets — the OS sends the closing packets. RabbitMQ sees the connection close
  immediately and requeues everything that was unacked on it, right then.
- **AMQP heartbeats, for the cases where TCP doesn't cleanly close.** A silent
  failure — network partition, power loss, a firewall dropping packets without
  resetting the connection — leaves no TCP close event for RabbitMQ to observe at all.
  Client and broker exchange small heartbeat frames periodically for exactly this
  reason; if the broker doesn't see one (or any traffic) within roughly twice the
  negotiated heartbeat interval, it decides the connection is dead, closes it from its
  own side, and requeues everything left unacked on it — same outcome as a clean crash,
  just detected differently.

Either way, the message that comes back around has its **`redelivered` flag set** —
visible on the message itself in RabbitMQ's management UI (`Redelivered: •` instead of
empty) — a durable record that this specific delivery isn't the first one.

**A direct consequence: at-least-once, not exactly-once.** A worker can finish the work
and crash *before* the ack reaches the broker — the work happened, but the broker
doesn't know that, and redelivers. So every consumer must be safe to run twice.
RabbitMQ (or any broker) cannot promise exactly-once delivery; only your code can make
duplicate delivery harmless.

**Prefetch controls how many un-acked messages a consumer can hold at once.**
Prefetch = 1 means "don't send me a second message until I've acked the first" — fair,
but a slow message blocks that consumer from taking on more. Prefetch = 50 means a
consumer can have 50 in flight, which is faster but means a crash loses (or, with late
ack, redelivers) up to 50 at once instead of 1.

## 5. Durability — what survives a broker restart

Two independent settings, both required, either missing means data loss on restart:

- **Durable queue/exchange** (declared at creation) — survives the broker process
  restarting. A non-durable queue is simply gone.
- **Persistent message** (delivery mode, set per message) — written to disk, not just
  held in memory. A durable queue holding non-persistent messages still loses them on
  restart; only the queue itself survived.

Both cost some throughput (disk I/O instead of memory-only), which is why they're
opt-in rather than the default.

## 6. Dead-lettering — what happens to a message nothing wants

A message can end up unroutable for several reasons: a consumer nacks it and gives up,
it sits past a TTL, or its queue hits a max-length limit. Left alone, RabbitMQ just
drops it. A **dead-letter exchange (DLX)** is a normal exchange you attach to a queue as
a fallback destination — messages that would otherwise vanish get republished there
instead, where a separate consumer (or a human) can inspect them.

```
queue A (main work)
   │  message nacked / TTL expires / queue full
   ▼
DLX ──▶ queue B (dead letters)
```

**The same mechanism doubles as a delayed-retry trick.** Give a message a short TTL and
a DLX pointing back at the original queue: it "expires" after N seconds and is
redelivered — a poor man's "retry in 30 seconds" with no extra scheduler. This is a
broker-native primitive; the equivalent in a plain task-queue library (like arq) has to
be built into the library instead, and in Kafka has no broker-level equivalent at all —
it's a convention (a separate `*.dlq` topic) rather than a feature.

## 7. Connections vs. channels — the thing that looks like a footgun but isn't

A **connection** is a real TCP connection — expensive to open, meant to be long-lived.
A **channel** is a lightweight virtual connection multiplexed over one real connection —
cheap, meant to be opened per logical unit of work (often per thread or per consumer).

The rule: **one connection per process, many channels within it.** Opening a new TCP
connection per message would work but is needlessly expensive; every client library
handles this for you as long as you don't fight it by manually opening connections in a
hot path.

---

## 8. How a task-queue library (Celery) actually uses this

Everything above is raw AMQP. A library like Celery hides most of it — which is exactly
why it's worth knowing what's underneath before relying on the defaults:

- **Celery creates its own queue automatically** for the common case — you don't
  hand-design exchange/binding topology just to run background tasks. That
  hand-designed topology is what an *event bus* needs (§3), not a task queue.

  **Worth checking the real exchange used, rather than trusting the worker's own
  startup banner.** A worker's log prints something like
  `.> celery exchange=celery(direct) key=celery`, which reads as "a direct exchange
  named `celery`." But inspecting an actual published message (RabbitMQ's management
  UI → the `celery` queue → "Get messages") shows `Exchange: (AMQP default)` — the
  message was actually published through RabbitMQ's **nameless default exchange**
  (every queue is auto-bound to it under its own name, per §2), not a named `celery`
  exchange at all. The banner is Celery/kombu's own internal display convention for its
  `Queue` object; the message you can actually pull off the wire is ground truth. When
  the two disagree, believe the message.

  That same message also confirms other things from earlier sections concretely rather
  than abstractly: `Redelivered` is unset on a first delivery (§4's redelivery flag,
  made visible), `delivery_mode: 2` is AMQP's code for *persistent* (§5 — Celery sets
  this by default, though the queue itself must also be durable for it to matter), and
  `correlation_id` carries the task's ID (Celery reuses AMQP's standard property for
  it rather than inventing a new one).

  **It also creates more than just that one queue — open the management UI after
  starting a worker and there are three, not one:**

  | Queue | What it's for |
  |---|---|
  | `celery` | The actual task queue — this is the one carrying real work (`ping`, `send_email`, etc.) |
  | `celery@<hostname>.celery.pidbox` | **Control plane**, one per running worker. Not a task queue at all — it's how remote-control commands reach that specific worker: `celery inspect ping`, `celery control shutdown`, revoking a task, adjusting rate limits at runtime. `AD` (auto-delete) means it disappears the moment that worker disconnects. |
  | `celeryev.<uuid>` | **Event/gossip traffic** between workers — the mechanism behind `mingle: searching for neighbors` in a worker's startup log, plus periodic heartbeats, plus what a monitoring tool like Flower would consume. A steady small message rate here is normal even with zero tasks ever enqueued, and even without Celery's `-E` flag — `-E` only controls *task*-level events (task-sent, task-succeeded), not this worker-to-worker channel, which runs regardless. |

  None of these three come from anything you configure — they're Celery's own internal
  machinery, created automatically the moment any worker starts, independent of what
  tasks actually exist. Seeing three queues after enqueueing exactly one task type
  isn't a sign anything is misconfigured.
- **`task_acks_late`** is Celery's on/off switch for §4's ack timing, and it **defaults
  to off** (ack-early). If the entire reason for choosing RabbitMQ was crash-safe
  redelivery, this setting is not optional — it must be turned on explicitly, or the
  default behavior silently throws away the guarantee you picked RabbitMQ for.
- **`worker_prefetch_multiplier`** governs §4's prefetch, scaled by worker concurrency.
- **Dead-lettering (§6)** isn't wired up by Celery automatically either — a failed task
  that exhausts its retries just fails; routing it to a DLX-backed queue is something
  you configure.

None of this is a criticism of Celery — it's the normal shape of any library built on
top of a lower-level protocol: it gives you sane defaults for the common case, and the
guarantees you actually care about are one config flag away, not the default.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Exchange | Receives published messages; routes them, stores nothing |
| Binding | Rule connecting an exchange to a queue |
| Queue | Where messages actually wait; the only piece with storage |
| Direct / Fanout / Topic / Headers | Exact match / broadcast to all / pattern match / match on headers |
| Ack / Nack | Consumer telling the broker "done" / "failed" |
| Ack early vs. late | Early = fast but loses work on crash. Late = crash-safe, must be configured |
| At-least-once | A crash before ack means redelivery — so every consumer must be idempotent |
| Prefetch | How many un-acked messages one consumer can hold at once |
| Durable (queue/exchange) + Persistent (message) | Both required for data to survive a broker restart |
| DLX (dead-letter exchange) | Fallback destination for messages nothing successfully consumes |
| TTL + DLX | Broker-native delayed-retry, with no extra scheduler |
| Connection vs. channel | One real TCP connection per process; many cheap channels multiplexed on it |
