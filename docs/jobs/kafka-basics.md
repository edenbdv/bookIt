# Kafka, in the amount of depth BOOK-48 needs

Deliberately general — no BookIt specifics. For what this project chose (topic
layout, partition key, what's deferred) and why, see `worker-decisions.md` and
BOOK-48/49/50/51 directly. Assumes `rabbitmq-basics.md` and `celery-basics.md` —
the contrast with what you already know there is most of what makes Kafka click.

## 0. The mental model, if you remember nothing else

```
              reservation_id
                     │
                     ▼
              Kafka message key
                     │
                     ▼
              SAME PARTITION
                     │
                     ▼
              SAME TOPIC
                     │
                     ▼
          ordering is guaranteed
```

```
                 booking-events                  ← topic
                       │
          ┌────────────┴────────────┐
          ▼                         ▼
 notifications group         analytics group     ← consumer groups (independent of each other)
          │                         │
     ┌────┴────┐               ┌────┴────┐
     ▼         ▼               ▼         ▼
    C1        C2              C3        C4       ← consumers (compete for partitions
                                                     only with others in their own group)
```

Same topic + same `reservation_id` key = ordering, for that reservation. Different
consumer groups = independent, full copies of the stream. Several consumers *inside*
one group = they split the partitions between them (competing, not duplicating).
Sections 2–5 below are this diagram, one piece at a time, with the concrete failure
case that shows why the first half isn't just a style preference.

## 1. The one-sentence reframe: a log, not a queue

Everything else in this doc follows from one difference. RabbitMQ's queue **removes**
a message once it's acked — the message is gone, consumed, done. Kafka's topic is an
**append-only log** — publishing a message just appends it, and consuming it never
removes anything. A message sits in the log for its whole retention period (hours,
days, however it's configured), regardless of who has or hasn't read it yet.

```
RabbitMQ queue:              Kafka topic (a log):
[msg][msg][msg] → consumed   [msg][msg][msg][msg][msg][msg] → still all there
      ↓ gone                                ↑
                                    consumers just point somewhere in it
```

This single fact is why Kafka can support replay (§7), why multiple independent
consumers don't compete for the same message (§5), and why there's no dead-letter
exchange (§8) — none of those make sense for a queue that deletes on consume, and all
of them fall out naturally from a log that doesn't.

## 2. Topics and partitions — where the log actually lives

A **topic** is a named log — `booking-events`, say. But a topic isn't one physical
log; it's split into **partitions**, each an independent, ordered, append-only log of
its own:

```
Topic: booking-events
┌─── Partition 0 ───┐   [msg][msg][msg][msg]
┌─── Partition 1 ───┐   [msg][msg][msg]
┌─── Partition 2 ───┐   [msg][msg][msg][msg][msg]
```

**Order is only guaranteed within one partition, never across partitions of the same
topic.** This is the single most important operational fact about Kafka, and the one
most likely to bite you if you don't design around it: two messages published to
different partitions have no guaranteed relationship in the order a consumer sees
them, even if you published the first one first.

## 3. The partition key — what actually decides where a message lands

Every message published to a topic carries a **key** (which can be empty). Kafka
hashes the key to pick a partition — the same key always lands on the same partition,
deterministically.

```
publish(topic="booking-events", key="reservation-42", value={...})
                    │
                    ▼
        hash("reservation-42") % num_partitions
                    │
                    ▼
            always partition 1 (say) -- every time, for this key
```

**This is the whole mechanism for ordering guarantees.** If every event about
`reservation-42` uses `"reservation-42"` as its key, every one of them lands on the
same partition, in publish order, and a consumer reading that partition sees them in
that order. Use no key (or an inconsistent one) and Kafka spreads messages round-robin
across partitions for throughput — fine for independent events, wrong the moment two
messages about the *same thing* need to stay in order relative to each other.

**A concrete failure case, because the key alone is a trap if the topic layout is
wrong.** Say two event types about the same reservation (`id = 123`) go to *separate*
topics — one per event type, a seemingly tidy design:

```
booking-confirmed                  booking-cancelled
    partition 2                        partition 2
        │                                   │
        ▼                                   ▼
   Confirmed(123)                      Cancelled(123)
```

Using `key = "123"` on *both* doesn't help — Kafka can order `Confirmed(123)` against
other messages **within `booking-confirmed`**, and `Cancelled(123)` against others
**within `booking-cancelled`**, but it has no mechanism at all for saying which of the
two came first. They're two unrelated logs; "partition 2" in one has no relationship
to "partition 2" in the other beyond sharing a number.

**The fix is one shared topic, not a cleverer key:**

```
booking-events
├─ Partition 0   ...
├─ Partition 1   Confirmed(123)
│                Cancelled(123)
└─ Partition 2   ...
```

Both events, same key, same topic → same partition → Kafka's ordering guarantee
actually applies, because now there's one log for it to apply *within*. Splitting
event types across topics is a real, common design instinct (it looks tidier, and
tools like schema registries often encourage it per-type) — but it quietly throws away
any ordering guarantee between those types, no matter how carefully the key is chosen
afterward.

## 4. Producers — simpler than RabbitMQ's model, on purpose

A RabbitMQ producer publishes to an **exchange**, which routes to zero or more queues
via bindings — a whole topology to design. A Kafka producer just writes to a
**topic** (partition chosen by key, §3) — there's no routing layer, no bindings, no
exchange types to choose between. This is the "different vocabulary, deliberately"
trade `worker-decisions.md` already names: you gain simplicity here, you lose AMQP's
routing flexibility (fanout to arbitrary queue sets by pattern-matching a routing key)
entirely. Kafka's answer to "who gets this" is topics and consumer groups (§5), not
routing rules.

## 5. Consumer groups — this is the concept that replaces both RabbitMQ patterns at once

RabbitMQ has two distinct patterns depending on topology: **competing consumers**
(several workers on one queue, each message to exactly one of them) and **fanout**
(an exchange copies a message to several independent queues, each with its own
consumer). Kafka gets both from a single mechanism: the **consumer group**.

**Within one consumer group, each partition is assigned to exactly one consumer** —
that's competing-consumers, automatically, by partition:

```
Topic: booking-events (3 partitions)     Consumer group "notifications"
Partition 0  ──────────────────────────▶ Consumer A
Partition 1  ──────────────────────────▶ Consumer B
Partition 2  ──────────────────────────▶ Consumer C
```

Fewer consumers than partitions means one consumer handles several partitions. More
consumers than partitions means the extras sit idle — **a partition can't be split
across two consumers in the same group**, which is why partition count is also your
ceiling on parallelism within one group. Concretely, with the 3-partition topic above:

```
Consumer A ← Partition 0
Consumer B ← Partition 1
Consumer C ← Partition 2
Consumer D ← (nothing -- no partition left to assign)
Consumer E ← (nothing -- same)
```

Running 5 consumers in that group doesn't get you 5-way parallelism — it gets you 3,
plus two idle processes doing nothing until a partition frees up. Partition count is
decided at topic creation and isn't casually changed afterward — it's a real ceiling,
not a runtime knob.

**Two different consumer groups reading the same topic each get the full stream,
independently** — this is fanout, for free, with no separate exchange or binding
config:

```
Topic: booking-events
        │
        ├──▶ Consumer group "notifications"  (its own offsets, own pace)
        └──▶ Consumer group "audit-log"      (its own offsets, own pace) -- future
```

Neither group affects the other's reading speed or position — this is the "multiple
independent consumers" benefit `worker-decisions.md` §9/BOOK-48 already point to, and
this mechanism is *why* it's true.

## 6. Offsets — how "where am I up to" actually works, and what a crash does to it

Each partition numbers its messages sequentially — the **offset**. A consumer tracks
the offset it's processed up to and periodically **commits** it (stored by Kafka
itself, keyed by consumer group + partition). On restart, a consumer resumes from its
last committed offset for that group — not from the beginning, not from "whatever's
new."

**This is where at-least-once delivery actually comes from, mechanically different
from RabbitMQ's ack/nack:** if a consumer processes a message, then crashes *before*
committing that offset, the next consumer to pick up that partition starts from the
old (uncommitted) offset — and reprocesses the same message. Same guarantee as
RabbitMQ (`rabbitmq-basics.md` §4's at-least-once), different mechanism entirely: no
per-message ack, just a periodically-advancing bookmark.

## 7. Retention and replay — the log's real superpower

Because consuming never deletes anything (§1), a message stays available for its
retention window regardless of consumption. A brand-new consumer group starting today
can read a topic **from the beginning** (or from any offset) and see everything still
retained — this is replay, and it's not a special feature bolted on, it's just what
"the log is still there" means. RabbitMQ has nothing equivalent: once a queue's
message is acked, it's actually gone.

## 8. No dead-letter exchange — and now it's obvious why

`rabbitmq-basics.md` §6's DLX reacts to a message being **nacked** — a consumer
explicitly refusing it, at the protocol level. Kafka has no equivalent concept: a
consumer doesn't ack or nack an individual message at all, it just advances (or
doesn't advance) its offset. There's nothing to "reject" a message *out of* — the
message was never "given" to a consumer in a removable sense, it's still sitting in
the log either way. A processing failure here has to be handled entirely in
application code: catch the error, and explicitly publish to a separate topic you
design for the purpose (a `*.dlq` topic) — there's no broker feature waiting to be
turned on.

## 9. KRaft — not central to using Kafka, worth knowing it exists

Kafka historically depended on ZooKeeper for cluster metadata and leader election.
**KRaft mode removes that dependency** — Kafka manages its own consensus internally,
so running it locally means one fewer service to stand up. Doesn't change anything
about topics, partitions, or consumer groups as covered above — purely an operational
simplification.

## 10. The CLI tools — every script lives at `/opt/kafka/bin/`, one job each

Kafka ships as a bag of shell scripts, not one command with subcommands (unlike, say,
`docker`). Each does exactly one thing. These are the ones actually used to build and
verify BOOK-48:

| Script | What it's for | What we used it for |
|---|---|---|
| `kafka-broker-api-versions.sh` | Asks a broker "are you alive and answering requests" | The `docker-compose.yml` healthcheck |
| `kafka-topics.sh` | Create/describe/list/delete topics | `--create` for `booking-events`; `--describe` to confirm 3 partitions |
| `kafka-console-producer.sh` | Publish messages typed on stdin, one per line | Sending test `Confirmed`/`Cancelled` messages with a `key:value` split |
| `kafka-console-consumer.sh` | Print messages from a topic to stdout | Reading them back with `--print.partition`/`--print.key` to see where each landed |
| `kafka-consumer-groups.sh` | List/describe consumer groups and their offsets | `--list` — confirmed **zero** real consumer groups exist yet, since the console consumer above doesn't register a durable one |

**The console producer/consumer are debugging tools, not application code** — nothing
in this project's `app/` calls them. They exist to let a human poke at a topic by
hand, the same role `psql` plays for Postgres or the RabbitMQ management UI plays for
queues. The real producer (the outbox relay, BOOK-50) and the real consumer
(Notification, BOOK-51) will be Python code using a Kafka client library, not shell
scripts — these are for verifying the broker itself works, independent of any
application.

**Why `kafka-console-consumer.sh --from-beginning` behaves the same every single run,
never "continuing from last time":** without an explicit `--group`, it generates a
fresh, throwaway consumer group name on each invocation. Kafka has never seen that
group before, so it has no committed offset to resume from — `--from-beginning` reads
the whole topic every time, and nothing is left behind afterward. This is *why*
`kafka-consumer-groups.sh --list` came back empty even after running the console
consumer several times: it was never joining a group that persists.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Topic | A named, append-only log. Doesn't delete messages on consume |
| Partition | One ordered sub-log of a topic. Order is guaranteed only within one, never across |
| Partition key | Determines which partition a message lands on (same key → same partition, always) |
| Consumer group | A named set of consumers; each partition goes to exactly one member — this is how Kafka gets both competing-consumers (within a group) and fanout (across groups) from one mechanism |
| Offset | A partition-local sequence number; a consumer group's position is "the last offset it committed" |
| Retention | How long a message stays in the log regardless of consumption — what makes replay possible |
| At-least-once (Kafka's version) | Crash before committing an offset → the next consumer reprocesses from the old offset. Mechanically different from RabbitMQ's ack/nack, same guarantee |
| No native DLX | Nothing to nack against — a log doesn't remove messages, so failure handling is entirely application code |
| KRaft | Kafka's own consensus, replacing ZooKeeper — operational simplification only |
| `kafka-topics.sh` | Create/describe/list topics |
| `kafka-console-producer.sh` / `-consumer.sh` | Manual debugging tools — publish/read by hand, not application code |
| `kafka-consumer-groups.sh` | List/describe consumer groups and their committed offsets |
| Console consumer's default group | A fresh, throwaway group per run (no `--group` given) — never resumes, never shows up in `--list` afterward |
