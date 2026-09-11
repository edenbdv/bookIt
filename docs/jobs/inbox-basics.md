# Inbox / idempotent-consumer pattern, in the amount of depth BOOK-51 needs

Deliberately general — no BookIt specifics. This is the consumer-side mirror of
`outbox-basics.md`; read that one first if you haven't, since several points here are
stated in contrast to it. For what this project's inbox table actually looks like and
why, see `worker-decisions.md` §13; for the concrete, current end-to-end flow, see
`booking-events-flow.md`.

## 0. The mental model, if you remember nothing else

```
 WITHOUT an inbox check                    WITH an inbox check

 message arrives                           message arrives
    │                                          │
 do the side effect                         "have I already done this
    │  (send email, charge card, ...)         exact message before?"
    │                                          │
 message redelivered                       yes -> skip, done already
 (broker retried, consumer                 no  -> do the side effect,
  crashed, rebalance...)                          record that it happened
    │
 do the side effect AGAIN
 (duplicate email, double charge...)
```

At-least-once delivery (every real broker's actual guarantee — see `outbox-basics.md`
§5, `kafka-basics.md`) means a consumer *will* see some messages more than once, sooner
or later. The inbox pattern is what turns "will occasionally reprocess" into "will
occasionally reprocess, but only *notices* it did so and skips" instead of blindly
redoing the side effect.

## 1. The dual-processing problem — the mirror image of dual-write

`outbox-basics.md` §1 covers the producer-side problem: writing to a DB and publishing
to a broker are two systems, so "both or neither" isn't automatic. The consumer side has
its own version of the same shape of problem:

```
1. do the side effect (send an email, call a payment API, ...)
2. mark this message as handled
```

These are two separate operations too — the side effect usually isn't part of the local
database transaction — and a crash between them has the same two failure modes as §1's
dual-write problem:

- **Side effect happens, then marking fails** (crash right after) — the message gets
  redelivered, nothing remembers the side effect already ran, and it runs *again*. A
  duplicate.
- **Marking happens, then the side effect never actually ran** (crash *before* doing it,
  but the mark write somehow still lands) — the message is now recorded as "handled,"
  and it never will be. A silent loss, worse than a duplicate.

Both are avoidable with the right ordering, covered in §4.

## 2. Why "just check if I've seen this message" isn't automatically safe

The instinctive fix — keep an in-memory set of message ids already processed, or check
some already-existing table before acting — doesn't hold up on its own:

- **In-memory state doesn't survive a crash or a restart**, and a consumer *will* restart
  (deploys, crashes, rebalances). Whatever "already seen" tracking exists has to be
  durable, not local variables.
- **A check followed by a separate action followed by a separate mark, with no
  transactional relationship between them, can itself race or fail halfway** — same
  reasoning as `outbox-basics.md` §2: three independent steps with no shared "all or
  nothing" boundary reintroduce the exact problem being solved, just one level down.

The fix isn't "remember more" — it's making the check-act-mark sequence itself resistant
to a crash landing in the middle, which §4 covers.

## 3. Two genuinely different cases, depending on what the side effect is

This is the point where the inbox pattern's guarantee depends on something outbox's
never had to: **what kind of thing the side effect is.**

**Case A — the side effect is itself just a local database write.** Then the check, the
write, and the "mark processed" row can all be **one local transaction** — the same trick
outbox uses in reverse. Either the whole transaction commits (effect + mark, together)
or none of it does. This gives a true, complete guarantee: the side effect happens
*exactly* as many times as the mark says it did, no window, no exception.

**Case B — the side effect is a call to something outside the local database** (send an
email, call a payment provider, hit another service's API). Now the check-then-act-then-
mark sequence necessarily spans a transaction boundary the external call can't be part
of — for the identical reason `outbox-basics.md` §2 gives for why a broker publish can't
be made part of a DB transaction. The best achievable order (§4) makes a duplicate *rare*
— a crash has to land in one specific narrow window — but not impossible. This is the
common case, and the one worth designing for explicitly rather than assuming away.

## 4. The core idea — check first, act, then mark (never mark first)

```
check: has this message's id been marked processed already?
    yes -> skip, nothing to do
    no  -> perform the side effect
           mark this message's id as processed
```

**Mark *after* acting, never before.** This is the one ordering choice that actually
matters:

| Order | What a crash between the two steps causes |
|---|---|
| Mark, then act | The mark says "done" while the side effect never ran — a **silent loss** |
| Act, then mark | The side effect already happened; a redelivery just risks running it again — a **duplicate** |

A duplicate is recoverable — the next delivery attempt (or a human) can still act on
it, notice it, or safely no-op it. A silent loss is not: nothing downstream knows the
side effect was supposed to happen and didn't. Between "sometimes duplicates" and
"sometimes silently nothing happens," the pattern always picks the former — same
asymmetry `outbox-basics.md` §5 makes for the producer side, applied to the consumer.

**The check itself doesn't need to be its own atomic gate against a concurrent race,
if the broker already prevents concurrent delivery of the same message.** Kafka's
consumer groups (`kafka-basics.md` §5) guarantee one partition is owned by exactly one
consumer at a time — so redelivery of a given message is always *sequential* (after a
crash and restart), never *concurrent*. A plain read-then-act-then-write is safe under
that condition. Systems where two workers really could race on the same message
concurrently (competing consumers with no per-key ownership, or plain HTTP retries) do
need the stronger insert-first-as-the-gate version, the same shape `IdempotencyKey`-style
tables use — see `idempotency-basics.md` for that variant and when it's actually needed.

## 5. Still not exactly-once for Case B — and that's the honest ceiling

For Case B (§3) — an external side effect — the act-then-mark order narrows the
duplicate window to "one call to something external, plus one local write," but does not
close it to zero. A crash landing in that exact gap still produces a duplicate. Nothing
about the inbox pattern itself can close that window further; doing so would require the
external system itself to cooperate — accepting an idempotency key on the call so *it*
also refuses to repeat the side effect twice (some payment processors, some email
providers do this). Absent that, "duplicates become rare, not impossible" is the actual,
final guarantee — not a gap to feel obligated to engineer away with more code on the
consumer's side alone.

## 6. Relationship to Outbox — same shape, opposite end of the pipe

| | Outbox | Inbox |
|---|---|---|
| Sits on | The producer side, before publishing | The consumer side, after receiving |
| Protects against | Publishing an event that a rolled-back transaction never actually earned | Redoing a side effect for a message that's already been handled |
| Local-only case | N/A — publishing to a broker is definitionally external | Case A (§3): side effect is just a local DB write — fully atomic, no window at all |
| External-call case | N/A | Case B (§3): side effect leaves the process — window narrowed, never fully closed |
| Ceiling | At-least-once delivery (`outbox-basics.md` §5) | At-least-once *processing* — duplicates minimized, not eliminated, for Case B |

Together they answer the two separate halves of "make an event-driven side effect
reliable": outbox makes sure the event definitely gets sent once something real
happened; inbox makes sure that if the same event shows up twice, it doesn't get acted
on twice.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Dual-processing problem | The consumer-side mirror of dual-write — "act" and "mark as done" are two separate operations, no shared transaction |
| Inbox table / idempotent consumer | A durable record of which message ids have already been handled, checked before acting |
| Case A (local-only side effect) | Check+act+mark in one DB transaction — fully atomic, zero duplicate window |
| Case B (external side effect) | Act, then mark — narrows the duplicate window to one external call, doesn't eliminate it |
| Mark after, never before | The one ordering choice that trades "occasional duplicate" for "never silently loses the side effect" |
| Check safety without an atomic gate | Safe with a plain read-then-write when the broker guarantees no *concurrent* redelivery of the same message (e.g. Kafka consumer groups) — otherwise needs the insert-first `IdempotencyKey` variant |
| Still at-least-once (processing) | Case B's ceiling — a real, external idempotency key on the call itself is the only thing that closes the window further |
