# Repository pattern basics (BOOK-26)

Written as theory before touching anything, same approach as `alembic-basics.md` and `exclude-constraint-basics.md` — updated in place once the actual BOOK-26 work happens, so it stays a real reference afterward.

**Prerequisite:** this assumes you've read [`sqlalchemy-basics.md`](../db/sqlalchemy-basics.md) — a repository is fundamentally "where the `Session`/`select()` calls live," so the vocabulary there (session, flush, identity map) is used freely below without re-explaining it.

## 1. The problem: `service.py` has nowhere to put a query except inline

Every module's `service.py` was, at the time this was written, an empty placeholder (see [`app/README.md`](../../app/README.md); `app/property/service.py` has real logic now — see [`router-service-basics.md`](router-service-basics.md)). The moment real business logic gets written there, it needs data — and without a repository, the only tool available is talking to SQLAlchemy directly: build a `select(Property).where(...)`, hand it to an `AsyncSession`, unwrap the result.

That's not wrong, exactly, but it fuses two different concerns into one function: *what the business rule needs* ("the property with this id, if it exists and isn't deleted") and *how SQLAlchemy expresses that* (`select`, `.where`, `.scalar_one_or_none()`). A repository is the seam that separates them.

## 2. What a repository actually is

One class per module, holding the query methods for that module's models — named for what's being asked, not how it's fetched. The truly generic CRUD (`get_by_id`, `list`, `add`, `delete`) lives once in `BaseRepository` ([`core/db/repository.py`](../../app/core/db/repository.py) — see section 3); each module's repository inherits that and adds only what's actually specific to it:

```python
# app/property/repository.py
from sqlalchemy import select
from app.core.db.repository import BaseRepository
from app.property.models import Property

class PropertyRepository(BaseRepository[Property]):
    model = Property

    async def list_filtered(self, limit: int, offset: int, city: str | None = None) -> list[Property]:
        stmt = select(Property).where(Property.deleted_at.is_(None))
        if city is not None:
            stmt = stmt.where(Property.city == city)
        stmt = stmt.order_by(Property.price_per_night_cents.asc(), Property.id.asc())
        stmt = stmt.limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return list(result)
```

(Simplified for illustration — the real `list_filtered` in `app/property/repository.py` also filters by `max_guests`/price range, added in `BOOK-29`; see [`router-service-basics.md`](router-service-basics.md).)

`get_by_id` and `add` aren't redefined here — they're inherited. `service.py` calls `PropertyRepository(session).get_by_id(...)` and never imports `select` itself. Every SQLAlchemy-specific decision for that module's models — which columns to filter on, how pagination is expressed, which relationships get eager-loaded — lives in exactly one file.

## 3. Why per-module, not one shared repository — and where `BaseRepository` fits

This is the same "package by feature, not by layer" reasoning [`app/README.md`](../../app/README.md) already makes for `models.py`/`schemas.py`/`service.py`/`router.py`: a `PropertyRepository` belongs in `app/property/`, a `ReservationRepository` in `app/reservation/`, and so on — not a single `app/repositories.py` holding every domain's queries mixed together. Same motivation both times: each domain's code stays in one place, and extracting a module later (Epic 11) doesn't mean untangling a shared file first.

That's a different thing from `BaseRepository` (`app/core/db/repository.py`). It's not "one repository for every domain" — it's a shared *base class* holding only what's identical across all of them (`get_by_id`, `list`, `add`, `delete`), so `PropertyRepository`/`ReservationRepository`/etc. don't each retype the same four methods. It carries zero domain knowledge — only "the model has an `id`" — so it belongs in `core/` the same way `Base` (`core/db/models.py`) and `get_db()` (`core/db/engine.py`) do: generic infrastructure every module already depends on, not a new kind of coupling *between* modules. `property/` still doesn't know `reservation/` exists.

**Bare shell now, methods added later — this isn't unusual.** Every module that owns a model (`UserRepository`, `ReservationRepository`, `PaymentRepository`, `ReviewRepository`, alongside `PropertyRepository`) already exists as a bare `BaseRepository[Model]` subclass with no domain-specific methods yet — those get added only once that module's `service.py` actually needs a specific query. This mirrors how other stacks do it: Spring Data JPA scaffolds `interface FooRepository extends JpaRepository<Foo, UUID> {}` the moment `Foo` is an entity, empty at first; EF Core's generic repository pattern and Django's model manager (`Foo.objects`) work the same way. The scaffolding is cheap and defined upfront; the real query logic is added lazily, per caller.

## 4. Why bother — what this actually buys, concretely

**Testability.** Right now, testing anything in `service.py` means testing against a real Postgres, because the query logic is inline — there's nothing to substitute. With a repository, a unit test can construct the service with a fake in-memory repository (a plain Python object implementing the same methods over a dict/list) and check the business logic in isolation — no DB, no Docker, fast. This is what the roadmap's Epic 10 ("Testing strategy & CI/CD") leans on for "unit tests for business logic (mocked repos)."

**One place for query-shape decisions that matter later.** [`concurrency-basics.md`](../db/concurrency-basics.md) and [`exclude-constraint-basics.md`](../db/exclude-constraint-basics.md) cover why the `EXCLUDE` constraint is the real defense against double-booking — but Epic 4's booking flow will still want a `SELECT ... FOR UPDATE` read as part of that flow (locking the row it's about to act on, belt-and-suspenders alongside the DB constraint). That's exactly the kind of decision that belongs in `ReservationRepository`, written once, instead of copy-pasted wherever `service.py` happens to need a reservation.

## 5. Where it does *not* help — not a database-swapping abstraction

This is about each module's *own* methods, not `BaseRepository`'s four generic ones (those are deliberately generic — see section 3). Beyond that floor, a repository is not an excuse to hide SQLAlchemy behind a generic `find(**filters)` dispatcher on the theory that Postgres might get swapped out later — it won't, and pretending otherwise just adds indirection nobody benefits from. Domain-specific methods stay specific to what the module's service code actually needs (`list_by_city`, not a generic filter-kwargs dispatcher). If a query is only ever used once, it's fine for it to look exactly like that one call site — don't invent generality no caller asked for.

## 6. The missing piece first: there's no session to inject yet

Every repository method above takes `session: AsyncSession` — but nothing in this codebase currently constructs one. [`core/db/engine.py`](../../app/core/db/engine.py) only has `get_engine()`, an `lru_cache`d `AsyncEngine`; the one place a DB connection is used today ([`core/router.py`](../../app/core/router.py)'s `/health`) opens a raw `engine.connect()` directly, not a `Session`.

FastAPI's usual answer is a `yield`-based dependency, now in [`core/db/engine.py`](../../app/core/db/engine.py):

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session
```

FastAPI runs the code before `yield` when a request starts, hands the yielded `session` to whatever depends on it, then resumes after `yield` (closing the session) once the request finishes — the same request-scoped lifecycle [`core/README.md`](../../app/core/README.md) already describes generically for "checkout, use, return" against the shared pool. A router route gets it via `Depends(get_db)`, then builds `PropertyRepository(session)` (directly, or via `service.py`) from that.

This is why BOOK-26 has to start here rather than at `PropertyRepository` itself — the repository's constructor is meaningless without something to hand it.

**For how `Depends(...)` + `yield` actually work, why the function is named `get_db` despite yielding a `Session`, and what closing the session does (and doesn't) close — see [`dependency-injection-basics.md`](../db/dependency-injection-basics.md).** None of that is specific to repositories, so it's kept as its own reference rather than repeated here.

## 7. Where this stops — Unit of Work is the next ticket, deliberately

A repository's job ends at "read/write this module's rows." It says nothing about *when a transaction commits*, or how to make writes to two different modules' repositories succeed-or-fail together (e.g., a reservation row and a payment row in Epic 8's saga). That's Unit of Work — [`unit-of-work-basics.md`](unit-of-work-basics.md), BOOK-27, kept as a separate ticket on purpose, layered on top of repositories rather than folded into them.

## 8. BOOK-26 status

1. ✅ `get_db()` in `core/db/engine.py` (section 6) — the session dependency every repository needs.
2. ✅ `BaseRepository[ModelT]` in `core/db/repository.py` (section 3) — `get_by_id`, `list`, `add`, `delete`.
3. ✅ `PropertyRepository` in `app/property/repository.py`, inheriting the base, adding `get_active_by_id` and `list_filtered` for `BOOK-29`.
4. ✅ `UserRepository`, `ReservationRepository`, `PaymentRepository`, `ReviewRepository` — bare `BaseRepository[Model]` shells (`app/auth/repository.py`, `app/reservation/repository.py`, `app/payment/repository.py`, `app/review/repository.py`), matching the AC's "one repository class per module that owns a model." Module-specific methods (beyond the inherited four) still only get added once that module's epic actually needs a query — the epics aren't organized per-module (checked: none of the 13 epics is "the Review epic"), so waiting for one to "naturally" prompt each repository isn't reliable.
5. No behavior changes to verify yet — this ticket is structural. The real test is BOOK-29 successfully calling `PropertyRepository` instead of touching `select()` directly.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Repository | One class per module, wrapping that module's models' queries behind business-named methods |
| `BaseRepository[ModelT]` | Shared base (`core/db/repository.py`) providing `get_by_id`/`list`/`add`/`delete` — generic, no domain knowledge, inherited by every module's repository |
| `service.py` | Calls the repository; never imports `select` or touches `AsyncSession` query-building itself |
| `get_db()` | The `yield`-based FastAPI dependency that hands each request a request-scoped `AsyncSession` — doesn't exist yet, first thing BOOK-26 needs. Mechanics: [`dependency-injection-basics.md`](../db/dependency-injection-basics.md) |
| Unit of Work (BOOK-27) | The separate concern of transactional boundaries spanning one or more repositories — not part of the repository itself |
