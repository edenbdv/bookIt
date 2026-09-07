# Outbox pattern, in the amount of depth BOOK-50 needs

Deliberately general — no BookIt specifics. For what this project's outbox table
actually looks like and why, see `worker-decisions.md`; for the DB-transaction
mechanism it piggybacks on, see `../domain-layer/unit-of-work-basics.md`.

## 0. The mental model, if you remember nothing else

```
 WITHOUT outbox                          WITH outbox

 DB.commit()                             DB.commit()
    │                                       │  (writes BOTH the domain
    ├─ succeeds                             │   change AND an outbox row,
    │                                       │   same transaction)
 broker.publish()  ← separate system,       │
    │                 separate failure         succeeds or rolls back
    ├─ succeeds? fails?                        as ONE atomic unit
    │
 two independent outcomes,                  relay reads unpublished rows
 no way to make them atomic                 later, publishes, marks done
```

In plain words: writing to a database and publishing to a message broker are two
different systems that don't share a transaction — so "do both, or do neither" isn't
something you get for free. The outbox pattern turns that into a problem you *can*
solve atomically, by writing the event to a table in the *same* transaction as the
data change, and moving the actual publish to a separate step that can fail and retry
without threatening the transaction at all.

## 1. The dual-write problem — why this is hard in the first place

Say a reservation gets confirmed, and confirming it should also publish a
`BookingConfirmed` event. The naive version does two things:

```
1. UPDATE reservations SET status = 'confirmed' ...
2. broker.publish("BookingConfirmed", ...)
```

These are two independent operations against two independent systems, and there is no
way to make both succeed or both fail together. Every ordering has a failure mode:

- **DB commits, then publish fails** (broker down, network blip, process crashes
  between the two lines) — the reservation is confirmed, but the event never goes out.
  Nothing downstream ever finds out.
- **Publish succeeds, then the DB write fails or rolls back** (a constraint violation,
  a crash before commit) — an event just told the world "this reservation is
  confirmed," and it isn't. Downstream systems now believe something that never
  happened, and there's no way to unpublish a message a consumer may have already
  acted on.

Neither failure is rare or exotic — a crash between two unrelated lines of code, or a
broker being briefly unreachable, is exactly the kind of thing retry policies
(`retry-basics.md`) exist to handle *within* one system. The dual-write problem is one
level up: it's not that either operation is unreliable, it's that there are *two* of
them with no shared notion of "both or neither."

## 2. Why "just publish inside the transaction" doesn't fix it

The instinctive fix is to call the broker from inside the same block as the DB write,
hoping that if anything fails, everything fails together:

```python
async with UnitOfWork() as uow:
    reservation.status = CONFIRMED
    await broker.publish("BookingConfirmed", ...)   # looks safe, isn't
```

This doesn't actually buy atomicity, for two separate reasons:

- **A network call has no transactional relationship to the DB transaction.** The
  broker doesn't know about the DB's commit/rollback — if the transaction later rolls
  back for an unrelated reason (a different constraint failing, a later line raising),
  the message has *already left the process*. There's no undo.
- **It couples the transaction's success to an external system's availability.**
  Now a broker outage — a system that has nothing to do with whether the reservation
  data itself is valid — can fail a DB write that would otherwise have succeeded fine,
  and the transaction sits open for however long the publish call takes or retries.

The publish and the DB write need to be atomic *with each other*, but a live network
call to a different system can never be made part of one local ACID transaction the
way two `UPDATE`s to the same database can.

## 3. The core idea — write the *intent* to publish, not the publish itself

The outbox pattern's move: stop trying to make the DB write and the broker publish
atomic with each other, and instead make the DB write atomic with **a local record of
what needs to be published** — something the database itself can guarantee, since it's
just another row in the same transaction:

```python
async with UnitOfWork() as uow:
    reservation.status = CONFIRMED
    uow.session.add(OutboxEvent(event_type="BookingConfirmed", payload=...))
    # one transaction, one commit, one atomic outcome for both rows
```

Both writes are now ordinary SQL against the same database, in the same transaction —
either both land, or (on rollback) neither does. There is no longer a moment where the
domain change exists but the "needs to be published" fact doesn't, or vice versa. The
actual publish to the broker is deliberately *not* part of this transaction at all —
it's deferred to a separate step (§4) that can fail, retry, and take as long as it
needs, without the domain transaction ever waiting on it.

## 4. The relay — a separate process that turns rows into real messages

Something still has to read the outbox table and actually call the broker. That's a
separate, independent process (often called a **relay**, **dispatcher**, or
**publisher**) with one job: find rows that haven't been published yet, publish them,
and mark them published.

Two common ways to build it:

- **Polling** — periodically query for `published_at IS NULL`, publish each, then set
  `published_at`. Simple, uses infrastructure the project already has (a scheduled
  job, same shape as any other periodic task), at the cost of some publish latency
  (bounded by the poll interval) and repeated queries against mostly-empty results.
- **Change Data Capture (CDC)** — a tool (e.g. Debezium) tails the database's write-ahead
  log directly and streams new outbox rows out without the table ever being polled.
  Lower latency, no polling overhead, but a real piece of extra infrastructure to run
  and operate.

Neither approach is "the" outbox pattern — the atomic write in §3 is the pattern; the
relay is just *a* consumer of the table it produces, and which kind you pick is a
separate, later decision based on the latency and operational cost you're willing to
take on.

**What each side of that trade actually costs, concretely:**

| | Polling relay | CDC (e.g. Debezium) |
|---|---|---|
| Latency | Bounded by the poll interval (typically seconds) | Near-immediate — reads the write the moment it's committed |
| Wasted work | A query every cycle even when nothing's pending (a partial index, §6, keeps this cheap) | None — it's only ever notified when a row actually changes |
| Missing a row | Not possible if the query is correct — every unpublished row is found on the next poll | Not possible either — it reads the log itself, not a snapshot |
| New infrastructure needed | None — reuses whatever already runs scheduled jobs | A CDC connector process (e.g. Kafka Connect + Debezium), run and monitored separately |
| Database changes required | None | The database must expose its write-ahead log for logical replication (e.g. Postgres's `wal_level=logical` plus a replication slot) — an actual server-config change, not just application code |
| Operational surface | Small — one more scheduled job among others already running | Larger — a new category of infrastructure (connectors, offsets, its own failure modes) to learn, deploy, and keep healthy |

CDC wins on latency and efficiency, unambiguously — but that win comes bundled with a
real, separate piece of infrastructure and a database-level config change, not just a
different code path. Whether that trade is worth it depends entirely on how much the
latency actually matters for what's being published, and how much operational surface
the team is willing to carry for it.

## 5. This is still at-least-once, not exactly-once — and that's the right trade

The relay itself isn't magically atomic either: it can crash *between* publishing a
message and marking the row published. When it comes back, that row still shows
`published_at IS NULL` — indistinguishable from a row that was never published at all
— so the relay publishes it again. The outbox pattern doesn't eliminate duplicate
publishes; it converts the failure mode from **"maybe never published"** (the dual-write
problem in §1) to **"maybe published twice."**

That trade is the point, not a limitation to route around: a lost event is
unrecoverable — nothing downstream can act on a message that never arrived, and there's
no way to know one is even missing without separately auditing both systems. A
duplicate event is recoverable *by design* — the same idempotency discipline this
project already applies everywhere else (`../db/idempotency-basics.md`,
`retry-basics.md` §4) means a consumer processing the same `BookingConfirmed` twice
just needs to treat the second one as a no-op, not a new fact.

## 6. Ordering — the relay can undo a partition key's guarantee if it's careless

A broker's per-key ordering guarantee (e.g. Kafka's, `kafka-basics.md` §3) only holds
for the order messages are actually **published in**. If two outbox rows for the same
key are published out of the order they were created — the relay processed them
concurrently, or read them without an `ORDER BY` on creation time — the broker will
still put them on the same partition, in the order it received them, which is now the
*wrong* order. The partition key guarantees "same key stays together," not "arrives in
creation order" — that second guarantee has to come from the relay publishing
in creation order in the first place.

## 7. Why not a real distributed transaction (2PC) instead?

Two-phase commit (2PC) is the "make two systems commit atomically for real" answer —
and it's usually not chosen over the outbox pattern, for reasons worth knowing even
without implementing it:

- Most message brokers (Kafka included) simply don't support participating in an XA/2PC
  transaction at all — there's no protocol-level hook for a broker to vote "prepared"
  and wait for a coordinator's final commit signal.
- Where it is supported (some brokers, some databases), it comes at a real
  performance and availability cost: a coordinator that all participants block on, and
  a "stuck between prepare and commit" state if the coordinator itself dies mid-decision.

The outbox pattern sidesteps this entirely by never needing the broker to participate
in the database's transaction — only the database is ever asked to be atomic, which is
a guarantee it already provides for free.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Dual-write problem | Two independent systems (DB, broker), no shared transaction — "both or neither" isn't automatic |
| Outbox table | A table written in the *same* local transaction as the domain change — the atomicity moves inside one database |
| Relay / dispatcher | A separate process that reads unpublished outbox rows, publishes them, marks them published |
| Polling relay vs. CDC | Poll the table on a schedule (simple, some latency) vs. tail the DB's write-ahead log (lower latency, more infra) |
| Still at-least-once | The relay can crash between publish and mark — outbox trades "maybe lost" for "maybe duplicated," which is recoverable |
| Ordering depends on the relay | A partition key keeps same-key messages together, but only *creation-ordered* publishing keeps them in the right order |
| 2PC (not chosen) | A real distributed transaction across DB and broker — most brokers don't support it, and it costs availability where they do |
