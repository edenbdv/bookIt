# SQLAlchemy / Postgres basics — A to Z

A reference for the concepts behind `app/core/db.py` and every `models.py`. Written for someone comfortable with REST APIs (`fetch()`, request/response) but new to talking to a database directly.

## 1. Who's who: three separate things

- **Postgres** — a completely separate running program (its own Docker container). Not part of your Python code. It only knows SQL.
- **SQLAlchemy** — a Python library that translates Python objects/method calls into SQL, and translates SQL results back into Python objects.
- **Your app** — writes Python (`User(...)`, `.add()`, etc.), never writes raw SQL directly.

Your app and Postgres are two separate processes. They only communicate by sending messages back and forth over a network connection — even though right now that network happens to be `localhost`, they're not "the same program" any more than your browser and a REST API are.

---

# Part A — defining a model

Before you can save anything, SQLAlchemy needs to know what your tables look like. That's what this part covers: how a Python class becomes a table definition.

## 2. `DeclarativeBase` and `Base` — the parent every model shares

`DeclarativeBase` is SQLAlchemy's base class for turning a Python class into a mapped database table. You don't use it directly — you subclass it **exactly once**, and that subclass (we call it `Base`, in `app/core/models.py`) is what every model in the whole app inherits from:

```python
# app/core/models.py
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass
```

```python
# app/auth/models.py
from app.core.models import Base

class User(Base):
    __tablename__ = "users"
    ...
```

**Why exactly once, and why it matters:** each class that subclasses `DeclarativeBase` *directly* gets its own private, independent registry of tables. If `User` inherited straight from `DeclarativeBase`, and `Property` also inherited straight from `DeclarativeBase` (instead of both inheriting from our shared `Base`), they'd each start their own separate catalog — and a `ForeignKey("users.id")` on `Property` would have no way to find `users`, because it lives in a completely different, disconnected registry. This isn't a hypothetical — it fails with a real error (`NullType`, "did you forget to specify a type on this Column?") when SQLAlchemy tries to generate SQL for that foreign key and can't resolve what it's pointing at.

So: one `Base`, defined once in `core/` (shared infrastructure, not owned by any single domain), imported by every module's `models.py`.

## 3. `metadata` — the catalog your models build

`Base.metadata` is the registry that results from defining models. It's not a metaphor — it's a real object with a `.tables` dict, filling itself in as each model class gets defined:

```python
Base.metadata.tables  # -> {}                          before any models are defined
# after `class User(Base): __tablename__ = "users"` runs:
Base.metadata.tables  # -> {"users": Table(...)}
# after `class Property(Base): __tablename__ = "properties"` runs:
Base.metadata.tables  # -> {"users": Table(...), "properties": Table(...)}
```

Each `Table` object holds real `Column` objects — name, type, whether it's a primary key, its foreign keys:

```python
Base.metadata.tables["properties"].columns
# -> [Column("id", UUID, primary_key=True), Column("host_id", UUID), Column("title", String)]

Base.metadata.tables["properties"].foreign_keys
# -> {ForeignKey(properties.host_id -> users.id)}
```

**Two things read this catalog:**
- `ForeignKey("users.id")` on any model — resolved by looking `users` up in this same shared registry
- **Alembic** — reads `Base.metadata` (what your code says the schema should be) and diffs it against the live database (what actually exists), then writes the migration for the difference

**The gotcha this creates:** a model only appears in `metadata` if its module has actually been *imported* somewhere. Defining a class doesn't register it until Python has run that file. If a `models.py` is never imported, Alembic never sees that table — and can conclude it shouldn't exist, generating a migration to *drop* it. Something has to import every model module in one place; we'll set that up in BOOK-23.

## 4. `Mapped[...]` / `mapped_column(...)` — one line, two jobs

```python
id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
```

**Left side — `id: Mapped[uuid.UUID]`** — a type annotation. Purely Python-facing: it tells you (and your editor/type-checker) that `some_user.id` will be a `uuid.UUID`. On its own, a bare annotation like this does nothing at runtime.

**Right side — `mapped_column(...)`** — configures the actual **database column**: primary key, foreign key, nullable, unique, defaults. SQLAlchemy also reads the annotation on the left to pick a matching SQL column type automatically (`uuid.UUID` → Postgres `UUID`, `str` → `VARCHAR`, etc.) — that's why you don't have to spell the SQL type out yourself.

You can see the two jobs split apart by writing the old, non-annotated form instead — fully equivalent, just without the annotation doing double duty:

```python
id: uuid.UUID                                          # Job A only: a bare Python type hint, does nothing at runtime
id = mapped_column(UUID(as_uuid=True), primary_key=True)  # Job B only: fully self-contained, explicit SQL type
```

The version we actually write fuses these: the annotation *is* the source of the SQL type, so `mapped_column()` doesn't need to repeat it.

**One more construct you'll see once models reference each other — `relationship()`:**

```python
reservations: Mapped[list["Reservation"]] = relationship(back_populates="property")
```

This is different in kind from `mapped_column()` — it configures **no column at all**. There's no `reservations` field in the `properties` table. It's a pure Python convenience: SQLAlchemy uses the foreign key that already exists on `Reservation` to compute this list on demand, so you can write `some_property.reservations` in code without writing the join yourself.

---

# Part B — talking to the database

Once models exist, this is how you actually read and write rows.

## 5. Round-trip

The same pattern as `fetch()`: send a request, wait, get a response. That wait is real — even on `localhost` it's not instant.

Every SQL statement — `INSERT`, `SELECT`, anything — is one round-trip: Python sends it to Postgres over the network, waits, gets a reply back. If you do 50 inserts one at a time, that's 50 round-trips, 50 waits. This is *why* ORMs try to batch things up instead of sending one statement per line of code — which is exactly what `Session` does (next section).

## 6. Transactions and isolation

A **transaction** is a group of changes that either **all** happen or **none** happen — no in-between state. You open one, do some work, then either `commit` (make it permanent) or `rollback` (throw it all away, as if none of it happened).

The guarantee that matters here is **isolation**: while a transaction is open, only *your* connection can see the changes it's making. Every other connection querying the database still sees the old state, as if nothing happened yet.

**Why that's the point, not a limitation:** say your code inserts a `Reservation`, then inserts its `Payment`, as two separate statements. If someone else queried the database in between those two statements, would you want them to see a reservation that exists with no payment? That's a broken, half-finished state. The transaction hides that "in progress" work entirely — nobody sees it until `commit`, when it all becomes visible **at once**. If the `Payment` insert fails, `rollback()` erases the `Reservation` too, so nobody ever sees the half-done version.

**Why this might feel new if you're used to a local DB:** many simple tools run in **autocommit mode** — every statement commits itself immediately, with no explicit transaction ever left open. It's not that transactions didn't exist; the tool just never gave you a reason to notice one was open.

## 7. You probably already have multiple connections open

A natural question: does this "other connections can't see it yet" thing actually come up, or is it theoretical?

It's already true on your own machine, right now. **DBeaver** holds a connection to Postgres. **The FastAPI app** holds a separate connection to the same Postgres. That's already two connections, from one person, at once — neither sees the other's uncommitted work. Open a `psql` terminal too and that's three.

At real scale this is constant: a live app serving many users means many requests hitting the same database at the same moment — which is exactly the double-booking race condition this whole project exists to solve. It's also *why* connection pooling exists (see `core/README.md`) — a pool holds several connections specifically because several things need to talk to Postgres simultaneously, not one at a time. Teams also often share a staging database for integration testing, though giving each developer their own local database (like we've done with Docker) avoids stepping on each other while coding.

## 8. `Session` — the staging area

`Session` is SQLAlchemy's middleman between your Python objects and Postgres. Think of it like a shopping cart, or a form you haven't submitted yet: things can sit in it, tracked, without anything being sent over the network.

```python
user = User(email="a@b.com", hashed_password="...")
session.add(user)
```

**Nothing has touched Postgres yet.** `add()` just tells the `Session`: "track this object, I intend to save it eventually." It's pure Python bookkeeping, zero round-trips.

## 9. `flush()` — hit submit

```python
await session.flush()
```

*Now* SQLAlchemy actually sends the pending `INSERT` (and any other pending changes) to Postgres — the first real round-trip. Postgres processes it and can hand back anything it generated (like a server-side `created_at`).

But it's still not final. Think of it like a server accepting your form and giving you a reference number, without yet officially processing it where everyone else can see it. In database terms: your changes exist inside an open **transaction** — visible to you, invisible to every other connection to the database.

## 10. `commit()` — final confirmation

```python
await session.commit()
```

This is what makes the change **permanent and visible to everyone else** — other connections, other requests, other people querying the same database now see it. Until this point, you could still back out entirely.

## 11. `rollback()` — the undo button

```python
await session.rollback()
```

If anything goes wrong before `commit()`, this discards everything pending in the transaction — as if none of it happened. This is the actual safety net that makes transactions useful: you can do several related changes (e.g. create a `Reservation` *and* a `Payment`), and if the second one fails, `rollback()` guarantees the first one never actually happened either. Nothing half-done gets left behind.

## 12. Client-side vs. server-side defaults

Ties directly into `id` vs. `created_at` in `app/auth/models.py`:

```python
id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

**`default=uuid.uuid4`** (client-side) — Python generates the value itself, in memory, the instant you write `User(...)`. Zero round-trips needed. `user.id` is already real before `add()`, before `flush()`, before anything touches Postgres.

**`server_default=func.now()`** (server-side) — Python sends nothing for this column; Postgres fills it in when it processes the `INSERT`. You only find out the actual value **after** a `flush()` — a round-trip is required just to learn it.

**Why the split, not "always pick one":**

| | Client-side (`default=`) | Server-side (`server_default=`) |
|---|---|---|
| Value available | Immediately, in Python, no DB needed | Only after `flush()` (a round-trip) |
| Best for | `id` — need it before persisting, want it in tests without a real DB, want it portable across DB engines | `created_at` — want the database's clock as the single source of truth, no matter what tool inserted the row |
| Cost | Doesn't apply if some other tool bypasses the ORM (raw SQL, another service) | Costs a round-trip to find out the value |

## 13. Worked example, line by line

```python
user = User(email="a@b.com", hashed_password="...")
# Pure Python. user.id is ALREADY set (client-side default ran locally).
# user.created_at is None — nobody's asked Postgres yet.
# Zero round-trips so far.

session.add(user)
# Still pure Python. Just staged in the Session. Zero round-trips.

await session.flush()
# FIRST round-trip. INSERT sent to Postgres. Postgres computes created_at
# and sends it back — now user.created_at is populated too.
# Still not visible to any other connection. Still inside a transaction.

await session.commit()
# Transaction finalized. Permanent. Every other connection can now see this row.
```

## 14. Migrations and transactions — same mechanism, different purpose

A natural question after Part A (structure) and Part B (transactions): are these related, or two separate topics?

**Related, but not the same thing.** Everything in Part B was about **data** — rows, `INSERT`/`UPDATE`/`DELETE` (this category of statement is called DML — Data Manipulation Language). Migrations change **structure** — tables, columns, constraints (`CREATE TABLE`, `ALTER TABLE`; this category is DDL — Data Definition Language). Different kind of statement, same underlying safety mechanism: Postgres can wrap structural changes in a transaction too. If a migration does several steps — add a column, then add an index, then add a constraint — and step 3 fails, Postgres rolls back steps 1 and 2 as well. You never end up with a half-applied schema sitting in the database.

This is a genuine Postgres strength worth knowing, not a given across all databases — MySQL, for example, auto-commits most structural changes immediately and can't roll them back the same way. It's part of why this project uses Postgres specifically for a project built around correctness under concurrency.

**One real caveat for later:** a few specific Postgres operations *can't* run inside a transaction at all — most notably `CREATE INDEX CONCURRENTLY`, used to add an index to a large, live table without locking it. Not a concern for our dev database now, but worth knowing it exists once BOOK-23's index (`properties(city)`) matters on a table with real production traffic.

So: migrations are fundamentally about structure, same as Part A — but *applying* one safely uses exactly the transaction mechanism from Part B, just aimed at DDL instead of DML.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `DeclarativeBase` | SQLAlchemy's base for mapped classes; subclass it exactly once |
| `Base` | Our one shared subclass (`core/models.py`) — every model inherits from this, not from `DeclarativeBase` directly |
| `metadata` | The catalog of every table your models define; what Alembic reads to generate migrations |
| `Mapped[T]` | Left side — the Python-facing type of an attribute |
| `mapped_column(...)` | Right side — the actual database column's configuration |
| `relationship(...)` | Not a column — a computed, in-Python convenience built from an existing foreign key |
| Round-trip | Send a message to Postgres, wait, get a reply — like `fetch()` |
| Transaction | A group of changes that all happen or none happen; isolated from other connections until committed |
| Isolation | Other connections can't see your changes until you `commit` |
| DML | Data Manipulation Language — `INSERT`/`UPDATE`/`DELETE`, changes to rows |
| DDL | Data Definition Language — `CREATE`/`ALTER TABLE`, changes to structure; what migrations are made of |
| `Session` | In-memory staging area for pending changes |
| `.add(obj)` | Stage an object — no network call |
| `.flush()` | Send pending changes to Postgres — first round-trip, still not permanent |
| `.commit()` | Make it permanent and visible to everyone else |
| `.rollback()` | Discard everything pending — undo |
| `default=` | Python computes the value, before touching the DB |
| `server_default=` | Postgres computes the value, when the row is inserted |
