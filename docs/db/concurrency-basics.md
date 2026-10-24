# Concurrency & architecture basics

Why several modeling choices in this codebase (like [`Payment`'s partial unique index](sqlalchemy-basics.md#11-index-vs-constraint-in-postgres-then-payments-partial-unique-index--proven-live)) favor a database-level rule over app-level coordination. This is general backend architecture, not SQLAlchemy-specific — it applies regardless of ORM, framework, or language.

## 1. "Handling concurrent requests" means interleaving, not serializing

A request handler that's waiting on I/O (a DB query, an HTTP call) hits an `await` and hands control back to the event loop instead of sitting idle — that's what lets one process serve many requests at once on limited hardware. But it also means two requests' steps can genuinely interleave: every `await` is a point where the runtime might switch to a different request's code before coming back to yours. "The framework handles concurrent requests well" and "two requests' code can run in an interleaved order" are the same fact, described from two angles — not a contradiction.

**Concrete example — two requests trying to charge the same reservation, no protection in place:**

| Time | Request A | Request B |
|---|---|---|
| t1 | `SELECT ... WHERE status='succeeded'` → none found | |
| t2 | | `SELECT ... WHERE status='succeeded'` → none found |
| t3 | `INSERT Payment(status='succeeded')` → succeeds | |
| t4 | | `INSERT Payment(status='succeeded')` → **also succeeds — nothing stopped it** |

Neither request did anything wrong in isolation — B's check at t2 was true *at the time*. The bug is that "check, then act" isn't one atomic step; anything can happen in between. This shape of bug is usually called **TOCTOU** (time-of-check to time-of-use), or a **race condition**.

## 2. Stateless app tier, stateful data tier

Application processes are typically **stateless and disposable** — any one of them can be killed, restarted, or duplicated without coordinating with the others, because none of them hold anything that matters long-term in memory. The **database is the one shared source of truth** that everyone agrees on.

**Why this matters for locks:** a lock implemented in Python (`asyncio.Lock`, `threading.Lock`) only coordinates code running *inside the same process's memory*. Two different processes have completely separate memory — neither can see the other's variables, objects, or locks, even running on the same machine. So an in-process lock only prevents the race in section 1 if both requests happen to land on the exact same process. It provides zero protection the moment there's more than one.

**Is that a real risk here, or a hypothetical?** Worth being precise rather than assuming: [`Dockerfile`](../../Dockerfile) currently runs a single `uvicorn` process with `--reload` (a dev convenience, no `--workers` flag) — so *today*, in this repo's dev config, it's genuinely one process. But that's a dev-only setting, not a guarantee. Production deployments of essentially any web service — monolith or microservice — commonly run multiple worker processes per container, multiple container replicas behind a load balancer, or both, purely for throughput and availability (one instance dying shouldn't take the whole app down). Code that's only correct "as long as there's exactly one process" breaks the moment that standard, ordinary scaling step happens. Correctness shouldn't depend on staying at the dev default.

## 3. What "shared" means for the database — not shared memory

Multiple processes reaching the same Postgres don't share memory. Each one opens its own **network connection** (TCP) to a separate, independently running program (see [`app/core/README.md`](../../app/core/README.md) for how connection pooling manages this per-process). "Shared" here means "the same external service, reachable by anyone with the connection string" — the same way many different browsers can all reach one REST API without sharing memory with each other or with the API server. [`sqlalchemy-basics.md`](sqlalchemy-basics.md)'s "you probably already have multiple connections open" section makes this concrete: your FastAPI app, DBeaver, and a `psql` terminal are already three separate processes/connections talking to the one Postgres, right now, on your own machine.

This is exactly why the database is the one place a rule can be enforced *no matter how many processes are running your code* — it's the only thing every process actually shares.

## 4. Truly simultaneous conflicting writes: Postgres serializes them automatically, for free

Section 1's race relied on both requests' `SELECT` running *before* either `INSERT` — a gap in time. A sharper question: what if two requests try to insert the exact same unique key at the *literal same instant*? Does Postgres actually do both at once?

**No — for that specific conflict, it doesn't.** Here's the real sequence, for two transactions (A and B) both inserting `kind='charge', status='succeeded'` for the same `reservation_id`:

1. A's `INSERT` starts, writes its row, and starts adding the corresponding unique-index entry.
2. B's `INSERT` starts moments later, trying to add an index entry with the *same key*.
3. Postgres sees the conflict at the index — but A hasn't committed yet, so it isn't settled whether A's row will actually end up existing. So **B's `INSERT` blocks and genuinely waits**, mid-statement, until A's transaction resolves.
4. If A commits: B resumes, sees a real, committed conflicting entry, and fails with `UniqueViolationError` — the exact error from the live test in [`sqlalchemy-basics.md`](sqlalchemy-basics.md#11-index-vs-constraint-in-postgres-then-payments-partial-unique-index--proven-live).
   If A rolls back instead: B resumes and succeeds — the conflict vanished along with A's aborted row.

So the two inserts aren't executed in parallel — B is made to wait its turn, automatically, as an inherent part of how a unique index maintains itself under concurrent writers. Nobody's code requested this waiting; it isn't `FOR UPDATE`, and it isn't a bug — it's just what "checking a unique index correctly under concurrency" requires.

**How can B "see" A's conflicting entry at step 3, if A hasn't committed yet?** This looks like it contradicts normal MVCC isolation — under a regular `SELECT`, a transaction genuinely can't see another transaction's uncommitted rows, by design. But index maintenance doesn't go through that same MVCC snapshot machinery at all. A B-tree/GiST index is a shared physical structure every inserting transaction writes into directly, regardless of commit status — it has to be, because uniqueness can only be enforced by comparing against *everything currently being inserted*, not just what's already durably committed. If the index only compared against committed rows, two transactions inserting the same key at nearly the same instant would each check, see nothing committed yet, and both proceed — the exact race this constraint exists to prevent. So index insertion is a deliberate, narrow exception to normal row-visibility rules: it's allowed to notice an in-flight, uncommitted conflicting entry specifically so it can block on it, rather than being blind to it and letting both writers through.

**This serialization is scoped only to the actual conflict, not the whole database.** A transaction inserting a payment for reservation X and another inserting an unrelated payment for reservation Y at the same instant share no key, so neither blocks on the other — both proceed with real parallelism across Postgres's separate backend processes. Postgres doesn't go single-threaded just because *something* concurrent is happening; only two writers racing for the *same* contested key ever get serialized this way.

### 4.1 `EXCLUDE` constraints (GiST) can deadlock this way — plain `UNIQUE` (B-tree) can't

`BOOK-39`'s concurrent-load script hit this for real, non-deterministically, on `Reservation`'s `no_overlapping_reservations` constraint:

```
asyncpg.exceptions.DeadlockDetectedError: deadlock detected
DETAIL: Process 2618 waits for ShareLock on transaction 1023; blocked by process 2631.
        Process 2631 waits for ShareLock on transaction 1021; blocked by process 2618.
```

This is a genuine cycle — each process ended up waiting on the *other* — which section 4's A/B walkthrough is structurally incapable of producing. The difference is where the serialization actually happens:

**`UNIQUE` B-tree: the physical write itself is the serializing point.** A given key has exactly one slot in the tree. When two transactions try to insert the *same* key, whichever is second is blocked at the act of writing to that slot — it never becomes a present-but-uncommitted row at all until the first resolves. Only one inserter can ever be "in the index" for that key at a time, so a wait can only ever point one direction (second waits for first). A cycle needs two things each waiting on the other; a single shared slot with a strict arrival order can't produce one.

**GiST `EXCLUDE`: two overlapping rows are two different physical entries, not one contested slot.** GiST indexes ranges in space, not exact-match keys — A's row and B's row live at different physical locations in the tree, so *both inserts can succeed and land, uncommitted, at the same time*, with neither blocking the other's write. Only afterward does each transaction scan for existing entries its own row overlaps — and that's where it can discover the *other's* uncommitted row and wait on it. If both scans happen to run around the same moment, each finds the other and waits for it — a symmetric, genuine deadlock, not a queue. Postgres's deadlock detector eventually kills one side to break the cycle, which is what turned into `DeadlockDetectedError` here.

**Why this used to turn into a raw `500` instead of a clean `409`:** `create_reservation`'s `except IntegrityError` doesn't catch it — `DeadlockDetectedError` surfaces as `sqlalchemy.exc.DBAPIError`, a different exception entirely. An early fix mapped that specific exception to `ReservationConflictError` too, but that's a lossy answer: a deadlock *victim* hasn't actually learned its dates are taken, just that it was the transaction Postgres picked to kill to break the cycle — its dates might still be free. Reporting that as "conflict" can wrongly tell a guest to give up on dates that were never really taken.

**The actual fix: remove the race, not just relabel its outcome.** `create_reservation` now takes a Postgres advisory lock scoped to `property_id` (`pg_advisory_xact_lock(hashtext(property_id::text))`) before inserting. This doesn't touch correctness — the `EXCLUDE` constraint is still what decides whether a booking is legal — it just serializes concurrent attempts *for the same property* so no two transactions ever hold uncommitted, overlapping GiST entries at once. That's the only situation the deadlock above can arise from, so with the lock in place it can't happen for this code path at all — not "less likely," structurally prevented. `except DBAPIError` was removed accordingly: there's no live case left for it to catch.

**Why this lock, specifically because it's GiST, not B-tree:** the whole reason a lock is needed here is section 4.1's distinction above — a `UNIQUE` B-tree index never needed this, because the physical write *is* the serializing point (only one transaction can ever be "in the index" for a given key at a time, per section 4). GiST has no such single contested slot for overlapping ranges — two overlapping rows are separate physical entries that can both land uncommitted simultaneously, which is exactly the gap the advisory lock closes by hand. A `UNIQUE` constraint gets that serialization for free from the index structure itself; a GiST `EXCLUDE` constraint doesn't, so this codebase has to add it explicitly.

**Why a Postgres lock and not a Redis one (`BOOK-43`):** the shortest version is that the window needing protection is `INSERT`→`COMMIT` *inside a Postgres transaction* — a boundary only Postgres can observe, and one `pg_advisory_xact_lock` tracks for free with no TTL to guess. `docs/cache/distributed-lock-basics.md` has the full argument, along with the general rule for when a Redis lock is the right tool and when it isn't.

## 5. `UPDATE ... WHERE` — atomic without your code ever requesting a lock

**The trap in the word "atomic": it does not mean "no lock is involved."** It means
Postgres's own internal locking collapses a check-and-write into one indivisible
statement, *without your application code ever asking for a lock*. That distinction —
who's responsible for the lock existing — is the entire point of this section, and
it's easy to misread "atomic" as "lock-free" when it really means "the lock is
Postgres's problem, not yours."

**The scenario this solves (BOOK-46's exact case):** a scheduled job wants to cancel a
reservation that's still `pending` and past its `expires_at` — but a payment could
confirm that same reservation in the moment between reading its status and writing the
cancellation. The naive version has a real gap:

```
1. SELECT -- is status = 'pending'?
2. if yes -- UPDATE status = 'cancelled'
```

Between step 1 and step 2, nothing stops another transaction from committing a change
to that exact row. The fix is to never let that gap exist as two separate statements:

```sql
UPDATE reservations
SET status = 'cancelled'
WHERE id = 123
  AND status = 'pending';
```

**This is one statement, not "check, then decide, then write."** You're not asking
Postgres "is it pending?" and then separately telling it to update — you're telling it
"update this row, but only if it's still pending *at the moment you actually touch it*,"
and letting Postgres itself perform that check as an inseparable part of the write.

**Walking through both possible orderings, with a concurrent payment confirmation:**

```
If the payment commits first:

  reservation: PENDING
        │
   Payment UPDATE  ──▶  CONFIRMED
        │
   Expiry UPDATE ... WHERE status = 'PENDING'
        │                       ↑
        │                 no longer true
        ▼
   0 rows affected — Postgres itself refused the write,
   because the WHERE clause no longer matches anything
```

```
If the expiry job commits first:

  reservation: PENDING
        │
   Expiry UPDATE  ──▶  CANCELLED
        │
   Payment's own logic now has to handle
   "this reservation is already cancelled" —
   a different problem, but not a silent data-corruption one
```

**The critical property: there is no moment where the expiry job says "I checked, it
was pending" and then acts on that stale belief later.** The check *is* the write —
they're not two steps with a gap between them for something else to happen in.

**Why this still counts as "atomic" even though nobody wrote `FOR UPDATE`:** Postgres
uses its own internal row-level locking to execute any `UPDATE` safely under
concurrency — that machinery doesn't disappear just because your code didn't ask for
it by name. The difference between this section and the next one isn't "locked vs.
unlocked":

| | `UPDATE ... WHERE` | `SELECT ... FOR UPDATE` (§6) |
|---|---|---|
| Is a lock involved? | Yes — Postgres's own, internal, automatic | Yes — explicitly requested by your code |
| Who manages it? | Postgres, for the duration of one statement | Your code, for as long as the transaction holds it |
| What it protects | One conditional write | A whole read-decide-write sequence, possibly spanning multiple statements or an external call |
| How you detect you lost the race | Check the affected-row count — `0` means someone else got there first | You don't race at all — the second transaction just waits its turn |

**One consequence worth stating plainly: "0 rows affected" is a real result you must
check, not an edge case you can ignore.** If the expiry job's code doesn't inspect how
many rows the `UPDATE` actually touched, it has no way to know the difference between
"successfully cancelled" and "skipped because someone else changed it first" — both
look like a statement that ran without raising an exception.

## 6. `SELECT ... FOR UPDATE` — a lock the database itself enforces

**Not used anywhere in this codebase today** — every invariant so far (double-booking, idempotency keys, payment dedup) turned out to be expressible as a single-table constraint instead, which section 7 explains is the better default when that's possible. Reach for `FOR UPDATE` directly when it *isn't* possible — the rule needs a multi-step read-then-decide-then-write sequence to happen atomically (not just one row's uniqueness at insert time), or spans multiple rows in a way no single constraint can express.

**A real future case: refund totals (Epic 8 / `BOOK-76`).** "Never refund more than a reservation was actually charged" is a sum across multiple `Payment` rows, not one row's uniqueness — a constraint can't express it. Two concurrent refund attempts could each sum the existing rows, both see the same amount still refundable, and both insert a refund that fits *individually* but doesn't together. `FOR UPDATE` on the reservation while summing, deciding, and inserting closes that gap the same way it does for the `Payment` example below.

```sql
BEGIN;
SELECT * FROM reservations WHERE id = X FOR UPDATE;   -- locks this row
-- ... check something, then INSERT/UPDATE related rows ...
COMMIT;   -- lock released
```

This is generic syntax, not a proposal for this codebase's `reservations` table specifically — `reservations` here is just a stand-in name to show the `FOR UPDATE` pattern in the abstract. The concrete example below, for `Payment`, is the one that actually engages with a real rule from this codebase.

Unlike an in-process Python lock, this **is** enforced by Postgres itself, so it does coordinate correctly across every process and replica — no shared-memory problem here. If a second transaction tries to take the same lock while the first still holds it, Postgres makes it *wait* until the first commits or rolls back, so the second transaction's later `SELECT` sees the post-write state instead of a stale one.

**What this would actually look like in this codebase**, had `Payment`'s "at most one succeeded charge per reservation" rule been enforced this way instead of with the partial unique index — illustrative only, this is *not* what the codebase does:

```python
# services/payment/src/payment/service.py -- the manual-lock alternative we did NOT choose

from sqlalchemy import select

async def charge_reservation(session, reservation_id, amount_cents: int) -> Payment:
    # 1. Lock the reservation row for the rest of this transaction. Any other
    #    request trying to charge the SAME reservation blocks here until this
    #    transaction commits or rolls back.
    reservation = (
        await session.execute(
            select(Reservation).where(Reservation.id == reservation_id).with_for_update()
        )
    ).scalar_one()

    # 2. Safe to check now -- no other transaction can be mid-way through the
    #    same check-then-insert for this reservation while we hold the lock.
    existing = (
        await session.execute(
            select(Payment).where(
                Payment.reservation_id == reservation_id,
                Payment.kind == PaymentKind.CHARGE,
                Payment.status == PaymentStatus.SUCCEEDED,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError("reservation already has a successful charge")

    # 3. Call the payment provider -- while STILL holding the lock from step 1;
    #    releasing it earlier would reopen the exact race we're preventing.
    result = await payment_provider.charge(amount_cents)

    session.add(Payment(
        reservation_id=reservation_id,
        kind=PaymentKind.CHARGE,
        amount_cents=amount_cents,
        status=PaymentStatus.SUCCEEDED if result.success else PaymentStatus.FAILED,
    ))
    await session.commit()   # lock released here
```

`.with_for_update()` is SQLAlchemy's way of adding `FOR UPDATE` to a `SELECT`. Two things to notice in this version, made concrete instead of abstract:

- **The lock is held from step 1 all the way through `commit()` in step 3** — spanning the entire `payment_provider.charge()` network call. That's the "holding it across an external call" cost stated plainly: any other request trying to touch this reservation (even just to look at it with its own `FOR UPDATE`) queues up and waits for however long the payment provider takes to respond.
- **Every single place that ever creates a `Payment`** — a retry job, a refund endpoint, a webhook handler for asynchronous provider callbacks, some future admin tool — has to reimplement this exact lock-then-check-then-insert sequence, in this order, correctly. Forget `.with_for_update()` in just one of them, and that one path has silently reintroduced the original race, indistinguishable from the paths that got it right.

Compare that to what the codebase actually has: [`services/payment/src/payment/models.py`](../../services/payment/src/payment/models.py)'s `Index(..., unique=True, postgresql_where=...)`. Zero code in `service.py` to get right or forget — the rule is enforced the instant *any* row is written, including by code that doesn't know this rule exists.

**So why isn't `FOR UPDATE` the default answer, if it genuinely works across processes?** Two real costs, illustrated above:

- **It only works if every code path that touches the locked resource remembers to take the lock, in the right order, inside a transaction.** A webhook handler, a retry job, a future endpoint, a one-off data-fix script — miss it once anywhere, and the protection silently stops applying to that path, while looking identical to the paths that do it correctly.
- **Holding it across an external call is expensive and risky.** If a charge attempt needs a network round-trip to a payment provider, holding a row lock open for that entire duration blocks every other operation on that row (cancellation, another read) for as long as the external call takes — which can be seconds, or much longer if the provider is slow.

## 7. Why a database constraint is preferred for correctness-critical invariants

**Both of these are enforced by Postgres, not Python — that's not the axis they differ on.** `FOR UPDATE` really does block a second transaction, exactly as advertised; there's no sense in which the *blocking itself* is weaker or less real than what a constraint does. The difference is **who is responsible for making the protection apply to a given write, and when that decision gets made:**

| | `FOR UPDATE` | Constraint / unique index |
|---|---|---|
| Who executes the actual block? | Postgres | Postgres |
| When is "protect this" decided? | Per query, in application code — a developer must write `.with_for_update()` on that specific `SELECT` | Once, in the schema — declared on the table, not on any particular query |
| Applies to a write only if... | ...that write's code path happens to include the lock | Always — there's no opt-in, because there's nothing to opt into |
| Depends on every writer remembering to use it? | **Yes** — one code path that skips the lock isn't protected, and nothing indicates it | **No** — Postgres checks every write unconditionally, no matter what wrote it |
| Cost | Holds a lock open, potentially across slow external calls | An instant check at insert/update time |
| Failure mode if forgotten | Silent — nothing indicates the protection is missing | N/A — nothing can bypass it |

A constraint isn't "safer" because it invokes some stronger enforcement than a lock does — both are just Postgres. It's safer because it's declared **once, on the table**, instead of being something a developer has to actively choose to invoke correctly on every single query that touches the data, forever. This is the same reasoning behind [`CheckConstraint`](sqlalchemy-basics.md#7-checkconstraint--rules-postgres-enforces-not-just-your-app) in this codebase: enforcement that doesn't depend on which code wrote the row is strictly more robust than enforcement that does.

## 8. Is this actually how real systems are built?

Yes — "stateless app tier, stateful data tier" is a standard, load-bearing pattern in backend engineering, not something specific to this project. It's the default assumption behind Kubernetes deployments, load-balanced container fleets, and serverless functions: application instances are cheap, interchangeable, and disposable; the database (or a dedicated coordination service, for things that truly need one) is the one thing treated as authoritative. The corollary is equally standard: **don't rely on in-process state for correctness that must hold system-wide** — it silently stops working the moment there's more than one process, which happens almost immediately in real deployments, often invisibly (a second worker, a second replica added for capacity, without anyone touching the "business logic").

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Race condition / TOCTOU | Two requests both read a "not yet done" state before either writes, so both proceed |
| Interleaving | What "handling concurrent requests" actually does — runs requests' code in overlapping order, not one-at-a-time |
| Stateless app tier | App processes hold nothing essential in memory; any one is disposable/replaceable |
| Stateful data tier | The database is the one thing every process/replica actually shares |
| In-process lock | Only coordinates code in the same process's memory — invisible to any other process |
| `UPDATE ... WHERE` (conditional update) | Check-and-write as one statement — atomic via Postgres's own internal locking, no explicit lock in your code. Check the affected-row count; `0` means you lost the race |
| `SELECT ... FOR UPDATE` | A row-level lock Postgres itself enforces — works across processes, but only if every writer remembers to use it |
| "Atomic" ≠ "no lock" | Atomicity can come from Postgres's own internal locking (conditional `UPDATE`) just as validly as from a lock your code explicitly requests (`FOR UPDATE`) — the difference is who's responsible for it existing, not whether one exists |
| DB constraint / unique index | Enforced by Postgres on every write, regardless of which code path wrote it — no discipline required |
| `UNIQUE` (B-tree) can't deadlock this way | One physical slot per key — the write itself serializes, so a wait can only ever point one direction |
| `EXCLUDE` (GiST) can deadlock | Overlapping rows are separate physical entries, both can land uncommitted at once, and each can discover + wait on the other — a real cycle (`BOOK-39` hit this on `Reservation`) |
| Advisory lock (`pg_advisory_xact_lock`) | An app-chosen lock key with no table/row behind it — used here to serialize concurrent inserts per `property_id` so the `EXCLUDE` deadlock above can't occur; doesn't replace the constraint, just removes the race that triggered the deadlock |
