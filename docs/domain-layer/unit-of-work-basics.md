# Unit of Work basics (BOOK-27)

Written alongside the actual BOOK-27 work (unlike some of the other docs in this repo, which were written as theory first) — updated in place if the implementation changes.

**Prerequisite:** this assumes you've read [`repository-pattern.md`](repository-pattern.md), specifically section 7 ("Unit of Work is the next ticket, deliberately") — this doc is that ticket.

## 1. The problem: nothing currently guarantees "both writes or neither"

`get_db()` ([`core/db/engine.py`](../../app/core/db/engine.py)) hands out one `AsyncSession` per request, but a `Session` by itself has no opinion about *when* to commit. If a service function does two writes through that session — e.g. creating a `Reservation` and its first `Payment` (Epic 8's saga) — nothing stops one succeeding while the other fails, unless every caller remembers to wrap its own `try`/`except`/`rollback()` by hand, every time.

**Concretely, without `UnitOfWork`, every place doing a multi-repository write repeats this by hand:**

```python
# without UnitOfWork -- every caller re-writes this same dance
async def create_reservation_with_payment(...) -> None:
    session = get_session_factory()()
    try:
        reservation_repo = ReservationRepository(session)
        payment_repo = PaymentRepository(session)

        reservation = Reservation(...)
        reservation_repo.add(reservation)

        payment = Payment(...)
        payment_repo.add(payment)

        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
```

**With `UnitOfWork`, the same operation becomes:**

```python
async def create_reservation_with_payment(...) -> None:
    async with UnitOfWork() as uow:
        reservation = Reservation(...)
        uow.reservations.add(reservation)

        payment = Payment(...)
        uow.payments.add(payment)
    # commit/rollback/close handled once, here -- not duplicated in every caller
```

The `try`/`except`/`finally` doesn't disappear — it moves from being copy-pasted into every service function into `UnitOfWork.__aenter__`/`__aexit__`, written exactly once.

## 2. What a Unit of Work actually is

A standard pattern (from Fowler's *Patterns of Enterprise Application Architecture*): one unit of work = one transaction = one `Session`. You enter a block, do any number of writes through any number of repositories, and on exit:

* no exception → commit everything
* an exception propagated → roll back everything, as if none of it happened

```python
# app/core/db/unit_of_work.py
class UnitOfWork:
    async def __aenter__(self) -> Self:
        self.session = get_session_factory()()
        self.users = UserRepository(self.session)
        self.properties = PropertyRepository(self.session)
        self.reservations = ReservationRepository(self.session)
        self.payments = PaymentRepository(self.session)
        self.reviews = ReviewRepository(self.session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        await self.session.close()
```

Usage, once a real `service.py` exists:

```python
async with UnitOfWork() as uow:
    uow.reservations.add(reservation)
    uow.payments.add(payment)
# clean exit here -> both committed together; an exception above -> both rolled back
```

## 3. Other frameworks provide this too — the difference is who wrote it

Unit of Work is standard practice; most ORMs already implement it under the hood (SQLAlchemy's `Session` itself is documented as an implementation of this pattern — it tracks pending changes and flushes/commits them together). What varies across stacks is **whether the framework ships the transaction-boundary tool for you, or you build it yourself** — none of them make it fully automatic with *zero* code, they just differ in how little code:

* **Django's `transaction.atomic()`** — still needs `with transaction.atomic():` written around your code, the same shape as `async with UnitOfWork():`. Django ships the tool; you still write the block.
* **Spring's `@Transactional`** — closer to automatic: one annotation on a method, and Spring wraps the entire call with begin/commit/rollback via a proxy, no explicit block in your code at all.
* **EF Core's `SaveChanges()`** — still an explicit call you make yourself, the same as calling `session.commit()`.

**Why SQLAlchemy doesn't ship one:** it's a toolkit, not an opinionated full-stack framework — it has no opinion on *where* a transaction boundary belongs (one HTTP request? one service call? a background job spanning several DB calls?). Django and Spring already made that call for you (per-request, or per-annotated-method). SQLAlchemy leaves it to the app to decide. `UnitOfWork` (`core/db/unit_of_work.py`) is us making that decision ourselves — "one unit of work = one block of business logic" — since nothing upstream of us already made it.

## 4. Why this works with zero changes to `BaseRepository`

Every repository attribute (`uow.reservations`, `uow.payments`, ...) is constructed from the *same* `self.session` object. A SQLAlchemy transaction belongs to the `Session`, not to any individual repository — so `uow.reservations.add(...)` and `uow.payments.add(...)` are automatically part of the same transaction without either repository knowing the other exists. `BaseRepository` ([`core/db/repository.py`](../../app/core/db/repository.py)) already just takes "an existing session" per its own docstring — `UnitOfWork` is simply the thing constructing and owning that session now, instead of `get_db()` or a script doing it ad hoc.

## 5. Why `__aenter__`/`__aexit__` specifically

This is Python's async context-manager protocol: `__aenter__` runs setup (open a session, build the repositories), your code runs inside the `async with` block, and `__aexit__` is *guaranteed* to run afterward — exception or not. That guarantee is exactly "always commit-or-rollback," the same thing a `try`/`finally` gives you, just spelled with `async with` because building it into a context manager makes the caller unable to forget the cleanup step.

**"Async" isn't why commit/rollback happens automatically — the *protocol* is.** The protocol exists in two flavors: sync (`with`, `__enter__`/`__exit__`) and async (`async with`, `__aenter__`/`__aexit__`). Both give the same "guaranteed cleanup" behavior; we need the async one purely because `session.commit()`/`rollback()`/`close()` are real network calls to Postgres that require `await`, and you can't `await` inside a plain sync `__exit__`.

**What actually calls these methods, and when:** the Python interpreter itself, the moment it parses `async with`. Writing

```python
async with UnitOfWork() as uow:
    uow.reservations.add(reservation)
```

is roughly equivalent to:

```python
uow_cm = UnitOfWork()
uow = await uow_cm.__aenter__()
try:
    uow.reservations.add(reservation)
except BaseException as e:
    await uow_cm.__aexit__(type(e), e, e.__traceback__)
    raise
else:
    await uow_cm.__aexit__(None, None, None)
```

Nothing inside `UnitOfWork` is "watching" for when to run — any class with methods named exactly `__aenter__`/`__aexit__` gets this treatment automatically from `async with`. That part is Python's guarantee, not our code.

**How the `with`/`async with` protocol actually works, step by step:**

1. Evaluate the object after `with`/`async with` (here, `UnitOfWork()`).
2. Look up `__enter__` (or `__aenter__`) **on the object's class**, not the instance — i.e. `type(obj).__enter__`, never `obj.__enter__`. This is enforced: setting `some_object.__enter__ = my_function` on an instance would **not** make `with some_object` work. It has to be defined on the class.
3. Call it. Whatever it returns gets bound via `as x`.
4. Run the indented block.
5. However the block ends — normally, or via an exception — look up `__exit__`/`__aexit__` on the class and call it, passing exception info (or `None, None, None` if nothing went wrong).

The same mechanism, isolated with no async and no database, using a plain sync context manager:

```python
class Demo:
    def __enter__(self):
        print("enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        print("exit, exc_type =", exc_type)

with Demo() as d:
    print("inside the block")
```
```
enter
inside the block
exit, exc_type = None
```

Now make the block raise:

```python
with Demo() as d:
    print("inside the block")
    raise ValueError("boom")
```
```
enter
inside the block
exit, exc_type = <class 'ValueError'>
Traceback ...
ValueError: boom
```

`__exit__` still ran even though the block raised — that's the entire guarantee this protocol provides. One more detail: if `__exit__`/`__aexit__` *returns a truthy value*, Python swallows the exception instead of re-raising it. `UnitOfWork.__aexit__` returns `None` (falsy), so an exception always propagates after rollback — the caller still sees the error, they just also get a guaranteed rollback first.

`async with`/`__aenter__`/`__aexit__` is the identical lookup and the identical steps above, just with `await` in front of each call, since ours need to do real I/O (`await self.session.commit()`, etc.) inside `__aexit__`.

**The commit-vs-rollback decision, on the other hand, *is* our code.** Python guarantees `__aexit__` gets called and passes it `exc_type` (the exception class, or `None` if the block finished cleanly) — what to do with that is entirely up to what we wrote inside `__aexit__`. An empty `__aexit__` would still get called every time; it just wouldn't do anything.

**Same shape as `get_db()`'s `yield`, different mechanism.** Both are "setup, caller's code, guaranteed cleanup" — `get_db()` uses `yield` because that's specifically what FastAPI's `Depends()` looks for; `UnitOfWork` uses `async with` because that's the general-purpose Python tool for this shape, usable anywhere, not just inside a FastAPI dependency. See [`dependency-injection-basics.md`](../db/dependency-injection-basics.md) for the `yield` side of this comparison.

## 6. One instance = one transaction, by construction

`__aenter__` creates a **brand-new** session every time it runs (`get_session_factory()()`). There's no way to accidentally reuse a session across two separate units of work, and no way to start writing through `uow.reservations` before `__aenter__` has run — `self.session` and the repository attributes don't exist until you're inside the `async with` block.

**Think of one `UnitOfWork` as a box around exactly one business operation:**

```
UnitOfWork
┌─────────────────────┐
│ one business op      │
│                       │
│ read                  │
│ update                │
│ update                │
│                       │
│ COMMIT ✅ / ROLLBACK ❌│
└─────────────────────┘
```

**Why a fresh box every time, rather than one reused across many operations —
concretely, what breaks if you don't:** say some job runs twice, back to back:

```
Correct -- a new UnitOfWork each run:

  Run #1 → UnitOfWork #1 → transaction #1 → COMMIT ✅
  Run #2 → UnitOfWork #2 → transaction #2 → ROLLBACK ❌

  Run #1's commit already happened and is final by the time
  run #2 even starts -- run #2 failing changes nothing about it.
```

```
Broken -- reusing one UnitOfWork across both runs:

  Run #1 ─┐
          ├─→ the SAME UnitOfWork → the SAME transaction
  Run #2 ─┘

  Run #2 failing means __aexit__ calls rollback() on the shared
  session -- which undoes run #1's changes too, even though run #1
  already succeeded and had nothing to do with run #2's failure.
```

There's a second, independent reason beyond that blast-radius problem: an
`AsyncSession` isn't safe for two operations to use *at the same time* — it holds one
checked-out connection and one in-flight transaction. Two concurrent callers sharing
one instance would be fighting over that single connection/transaction, not just
risking a confusing rollback.

**This is exactly why every caller — API or worker — constructs its own `UnitOfWork`,
never a shared one:**

| | Who constructs it | How |
|---|---|---|
| API route | FastAPI, via `Depends(get_uow)` | `get_uow()` is a thin `async with UnitOfWork() as uow: yield uow` wrapper — same class, shaped as a generator so `Depends()` knows how to call it and clean up after the response |
| Celery task (`docs/jobs/worker-decisions.md` §5) | The task function itself | `async with UnitOfWork() as uow:` written directly in the task body — there's no request, so nothing plays the role `Depends()` plays in a route |

Same rule either way: **one independent operation, one `UnitOfWork`, constructed fresh
inside that operation** — never hoisted out to somewhere it could get reused across
more than one.

## 7. `uow.session` is public, on purpose

`seed.py`'s `get_or_create` helper is generic across *any* model (`model: type[ModelT]` is a parameter) — it isn't "the reservation module's queries," so it doesn't fit inside any one repository. It needs the raw session directly, which is why `UnitOfWork.session` is a public attribute (`self.session`, not `self._session`) — matching `BaseRepository.session`'s own public convention, not a special case.

## 8. What replaced what

`app/core/db/seed.py`'s `main()` used to build its own engine, open a bare `AsyncSession`, call `seed()`, commit, then dispose the engine by hand:

```python
# before
async def main() -> None:
    engine = get_engine()
    async with AsyncSession(engine) as session:
        await seed(session)
        await session.commit()
    await engine.dispose()
```

Now:

```python
# after
async def main() -> None:
    async with UnitOfWork() as uow:
        await seed(uow)
```

`seed()` itself takes `uow: UnitOfWork` instead of `session: AsyncSession`, and pulls `session = uow.session` internally for `get_or_create`.

## 9. Where this stops — `service.py` still isn't real yet

The AC's last line — "`service.py` functions receive a `UnitOfWork` ... instead of importing `get_engine()`/`AsyncSession` directly" — wasn't demonstrated by real business logic at the time this was written, because every module's `service.py` was still an empty placeholder (same situation `repository-pattern.md` section 1 describes). It is now, for `property/service.py` — see [`router-service-basics.md`](router-service-basics.md) for the full request-scoped `get_uow()` dependency this section originally only sketched.

## 10. BOOK-27 status

1. ✅ `UnitOfWork` in `app/core/db/unit_of_work.py` — wraps one session, exposes `users`/`properties`/`reservations`/`payments`/`reviews`.
2. ✅ Async context manager — commit on clean exit, rollback on exception, always closes.
3. ✅ One instance = one transaction = one session, by construction (fresh session built in `__aenter__`).
4. `service.py` receiving a `UnitOfWork` — deferred until a real one exists (see section 8).
5. ✅ `app/core/db/seed.py` refactored to use `UnitOfWork` instead of its own engine/session/commit/dispose sequence.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Unit of Work | One transaction, one `Session`, coordinating writes across any number of repositories |
| `UnitOfWork` | `core/db/unit_of_work.py` — async context manager wrapping a session + every module's repository |
| `__aenter__` / `__aexit__` | Async context-manager protocol: setup, caller's code, guaranteed cleanup (commit or rollback) |
| `uow.session` | Public, like `BaseRepository.session` — needed by generic helpers (e.g. `seed.py`'s `get_or_create`) that aren't scoped to one repository |
| One instance = one transaction | Every `UnitOfWork()` builds a fresh session in `__aenter__` — no accidental reuse across units of work |
