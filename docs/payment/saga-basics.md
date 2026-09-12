# Saga pattern, in the amount of depth BOOK-54 needs

Deliberately general — no BookIt specifics. For what this project's saga actually looks
like and why, see `payment-decisions.md`; for the broader family this pattern belongs
to, see `../jobs/outbox-basics.md` and `../jobs/inbox-basics.md` — same underlying
problem, three different answers to it.

## 0. The mental model, if you remember nothing else

```
 A single transaction                    A saga

 BEGIN                                   step 1 (local tx) -- commits
   do step 1                                 │
   do step 2                                 ▼
   do step 3                             step 2 (crosses outside the DB --
 COMMIT (all or nothing)                 an external call, can't be "in"
                                          any transaction)
                                              │
                                     succeeds ┴ fails
                                              │         │
                                              ▼         ▼
                                        step 3       compensate step 1
                                        (local tx)   (a separate local tx
                                                       that undoes it)
```

A saga is what you reach for when a multi-step process *can't* be one transaction,
because one of the steps isn't a database operation at all — and you still need the
overall process to end up consistent.

## 1. Why "just use a transaction" doesn't apply here

A database transaction gives you all-or-nothing for free, but only for operations the
database itself can undo — rows it wrote, indexes it updated. It has no power over
anything outside itself. The moment one step in a process is a call to another system —
another service's database, an external API, a payment provider — that step can't be
inside your transaction, for the identical reason a broker publish can't be
(`outbox-basics.md` §2): the other system has no idea your transaction exists, and a
`ROLLBACK` on your side does nothing to whatever already happened over there.

So "wrap it all in one transaction" isn't a design choice being declined here — it's
not an option once a step crosses that boundary at all.

## 2. The core idea — forward steps, each committed on its own, plus an explicit undo

```
step 1 (local, commits)  →  step 2 (external)  →  step 3, reacting to step 2's result
```

Each local step commits for real, independently. If a later step fails, the saga
doesn't try to un-happen an earlier step's commit — it can't, that transaction is long
closed. Instead, it runs a **compensating action**: a new, separate operation whose job
is to semantically undo the earlier step's effect (release a hold, issue a refund,
cancel an order) — not a database-level rollback, a business-level reversal.

This is the entire pattern. Everything else below is refinement of this one idea.

## 3. When a saga is actually needed — not "distributed system," but "a step with a real effect to undo"

The saga pattern is usually introduced alongside microservices, because a microservice
architecture guarantees the trigger on *every* step: separate services almost always
mean separate databases, so no step can share a transaction with the next one. But
microservices aren't what makes a saga necessary — crossing a non-transactional
boundary is, and that can happen with exactly one external call in an otherwise
ordinary single-database system.

Two questions decide whether a given step needs saga treatment at all:

1. **Does something downstream depend on this step's outcome to decide what to do
   next?** If nothing later in the process branches on whether this step succeeded or
   failed, it's a terminal action, not a decision point — there's no "then what" for a
   saga to coordinate.
2. **Does a *successful* outcome leave behind a real effect worth undoing if the rest
   of the process can't complete?** A step that succeeds and leaves nothing behind (an
   email delivered, a log line written) has nothing for a compensating action to
   reverse, even in principle — no saga needed for it, ever, regardless of where it
   sits or what triggered it.

Only a step that's both a real decision point *and* produces a real, external effect on
success needs a compensating action. A step that's external but is neither of those
(most notifications) just needs the durability/dedup machinery `outbox-basics.md` and
`inbox-basics.md` already cover — not a saga.

## 4. Orchestration vs. choreography — two ways to actually build one

**Orchestration.** A single coordinator calls each step in sequence and explicitly
decides which compensating action to run on failure. Easy to read start to finish in
one place; that same place is a single point that has to know about every step.

**Choreography.** No central coordinator — each step reacts to an event the previous
step published, and publishes its own event when it's done. No single place owns the
whole sequence; instead the sequence emerges from independent services each reacting to
what came before. Requires an event bus (Kafka, etc.) to already be in place — that
infrastructure is the whole mechanism choreography runs on.

Neither is "the" saga pattern — both satisfy the same forward-steps-plus-compensation
shape; the choice is about how visible/centralized the sequencing is, not about
correctness.

## 5. Idempotency — a saga's own steps must tolerate being re-entered

A saga step's trigger is frequently something that can fire more than once for the
same logical attempt — a retried request, a redelivered event, a webhook firing twice.
If step 3 (react to step 2's outcome) isn't itself safe to run twice, a redelivery can
try to apply the same forward or compensating action to state that's already been moved
past `PENDING` (or whatever the starting state was) — and a naive state-machine
transition will usually reject that outright, not silently double-apply it. Treating
"the state has already moved on" as a no-op, not an error, is what makes a saga step
survive redelivery the same way `inbox-basics.md`'s dedup check does.

## 6. The gap no amount of local commit ordering closes

Splitting a saga's steps into separate local transactions (§2) fixes *one* failure
window: a later step failing no longer erases an earlier step's already-committed
record. It does not, and structurally cannot, fix an earlier window: the process
crashing **between the external call returning and that first local commit actually
landing**. In that window, the external system has already recorded its side effect —
committed nothing on our end has any way to know that happened.

This is the honest, irreducible core of the distributed-transaction problem: two
systems, no shared transaction, and *no ordering of our own local commits* can make our
database durable at the exact instant an external system's state changes, because that
instant isn't under our control at all. Every mitigation below manages this gap; none
of them eliminate it.

- **Idempotency keys sent *to* the external system, not just received from our own
  clients.** If the local process crashes and a retry re-attempts the same logical
  operation, the external system recognizes the repeat (by the same key) and returns
  its original result instead of repeating the effect — the same idea `inbox-basics.md`
  applies to us, pushed one hop further out, onto a system we don't control.
- **Treat an inbound webhook/callback as the source of truth, not just the answer to
  a `TIMEOUT`.** A webhook fired by the external system is unaffected by whether *our*
  process survived long enough to record anything — it's an independent, later
  confirmation of what the external system actually did, and remains true even if
  we crashed before our own attempt was ever durably recorded. This only works if
  whatever correlates the webhook back to our own record (an id we control) was handed
  to the external system *before* the crash could happen — at the start of the call,
  not after a response we might never durably process.
- **Reconciliation.** A periodic job comparing our own records against the external
  system's own list of what it actually did, for the residual cases neither of the
  above catches on their own — the last-resort net under both.
- **Compensation (refund or equivalent) as the actual last resort**, once reconciliation
  finds an effect on the external side with no matching local record at all.

None of these make the gap zero-width. They make it survivable — the same posture this
whole family of patterns already takes toward at-least-once delivery (`outbox-basics.md`
§5, `inbox-basics.md` §5): not chasing an impossible guarantee, narrowing the window and
making what gets through recoverable instead.

---

## Cheat sheet

| Term | Meaning |
|---|---|
| Saga | A multi-step process, each step a local transaction, with explicit compensating actions instead of automatic rollback |
| Compensating action | A new operation that semantically undoes an earlier committed step — not a DB rollback |
| The real trigger | A step crossing outside the local transaction (an external call) — not "how many services," not "how many databases" |
| Decision point vs. terminal action | Only a step something downstream branches on needs saga treatment at all |
| Orchestration | One coordinator, explicit sequencing and compensation — simple to read, centralizes the sequence knowledge |
| Choreography | No coordinator — each step reacts to the previous step's event — needs an event bus already in place |
| Saga idempotency | A redelivered/retried step must treat "already moved past this state" as a no-op, not an error |
| The remaining gap | Crash between the external call and the first local commit — no local commit ordering can close this |
| What manages it | Provider-side idempotency keys, webhook-as-source-of-truth (with a correlation id sent up front), reconciliation, compensation as last resort |
