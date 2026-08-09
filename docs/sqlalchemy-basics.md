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

**You can skip `mapped_column()` entirely when there's nothing to configure.** Plenty of fields in this codebase have no `= mapped_column(...)` at all:

```python
# app/property/models.py
title: Mapped[str]
description: Mapped[str]
country: Mapped[str]
```

SQLAlchemy's declarative system scans every attribute for the `Mapped[...]` wrapper specifically (not just any type hint — a bare `title: str` with no `Mapped[]` and no assignment stays an inert type hint, not a column). When it finds `Mapped[X]` with nothing assigned, it implicitly does `= mapped_column()` for you — a plain, `NOT NULL` column of the SQL type matched from `X`, no PK/FK/index/default. So `title: Mapped[str]` is exactly equivalent to `title: Mapped[str] = mapped_column()`; the explicit call is just redundant when there's no config to pass.

You only need to write `mapped_column(...)` out when a field needs something a plain column can't express — a primary key, a foreign key, `unique=`/`index=`, a `default=`/`server_default=`, an explicit SQL type, or nullability (`Mapped[X | None]`). Every field in `User` and `Property` that calls `mapped_column()` explicitly is doing so for exactly one of those reasons (see `id`'s `primary_key=`, `email`'s `unique=`/`index=`, `host_id`'s `ForeignKey(...)`, `created_at`'s `server_default=`, etc.) — the ones that don't need any of that just skip it.

**`index=True` vs `unique=True` — two independent things, often paired:**

```python
email: Mapped[str] = mapped_column(unique=True, index=True)
```

- **`index=True`** builds an actual Postgres index — a separate structure that makes `WHERE email = ...` fast (roughly O(log n) row lookup instead of scanning every row). Login does exactly this query on every request, so `email` needs it. `city` on `Property` has the same reasoning (see its comment: filtered on directly in availability search).
- **`unique=True`** is a *constraint*, not a speed feature — Postgres rejects an `INSERT`/`UPDATE` that would create a duplicate value. It's about correctness (no two users share an email), not query speed.

They're often set together, but you can have either alone — `city` is `index=True` with no `unique=True` (many properties share a city, but lookups on it should still be fast).

**A single-column `index=True` on two different columns does *not* create one combined index.** `email: mapped_column(index=True)` and, hypothetically, `role: mapped_column(index=True)` would give you two separate single-column indexes — useful for `WHERE email = ...` or `WHERE role = ...` alone, but not for `WHERE email = ... AND role = ...` together.

**To index multiple columns *together* (a composite/paired index), use `Index(...)` in `__table_args__` instead** — same tuple that already holds `CheckConstraint` in `Property`:

```python
from sqlalchemy import Index

class Property(Base):
    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint("max_guests > 0", name="max_guests_positive"),
        CheckConstraint("price_per_night_cents >= 0", name="price_non_negative"),
        Index("ix_properties_city_country", "city", "country"),
    )
```

This builds one index spanning both columns, which Postgres can use far more efficiently than two separate single-column indexes when a query filters on both together (e.g. `WHERE city = ... AND country = ...`). Column order matters — a composite index on `(city, country)` speeds up queries that filter by `city` alone or by `city AND country` together, but doesn't help a query that filters by `country` alone (this is the "leftmost prefix" rule: Postgres can only use the index starting from its first column). Not needed yet in this codebase — worth reaching for once a query actually filters on a combination of columns together, not before.

**One more construct you'll see once models reference each other — `relationship()`:**

```python
reservations: Mapped[list["Reservation"]] = relationship(back_populates="property")
```

This is different in kind from `mapped_column()` — it configures **no column at all**. There's no `reservations` field in the `properties` table. It's a pure Python convenience: SQLAlchemy uses the foreign key that already exists on `Reservation` to compute this list on demand, so you can write `some_property.reservations` in code without writing the join yourself.

## 5. `relationship()` and `back_populates` — the bidirectional link

Take the actual pair already in this codebase:

```python
# app/property/models.py
host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
host: Mapped["User"] = relationship(back_populates="properties")
```

```python
# app/auth/models.py
properties: Mapped[list["Property"]] = relationship(back_populates="host")
```

Two attributes exist on `Property` that look related — `host_id` and `host` — but only one of them is a real column:

| | `host_id` | `host` |
|---|---|---|
| What it is | A real column — `mapped_column(ForeignKey(...))` | Not a column — `relationship(...)` |
| Value | A raw UUID | A full `User` object |
| Where it comes from | Sent in the `INSERT`, stored in Postgres | Computed by SQLAlchemy from `host_id`, on demand |
| Cost to read | Free — already in memory | May cost a round-trip (see below) |
| Use it to | Filter/compare (`.where(Property.host_id == id)`) | Read the user's data (`property.host.email`) |

**Why `host` can cost a round-trip:** by default, `relationship()` is lazy-loaded. The first time you access `some_property.host`, if that `User` isn't already sitting in the `Session`, SQLAlchemy sends a `SELECT` right then to fetch it. Reading `some_property.host_id` never does this — the UUID is already in memory, no query needed.

**What `back_populates` buys you:** each `relationship()` call names the matching attribute on the *other* class — `Property.host` says "my other half is `User.properties`," and `User.properties` says "mine is `Property.host`." SQLAlchemy uses that pairing to keep both sides in sync **in memory, with no extra query**:

```python
some_property.host = some_user
# some_user.properties now includes some_property too — pure bookkeeping, no SELECT.
```

Leave `back_populates` off one side and you'd have two relationships that don't know about each other — setting one wouldn't update the other.

**Why this actually matters (it's not about writing less code):** you still write `relationship()` on both classes either way — `back_populates` doesn't save you a declaration. What it prevents is your **in-memory objects disagreeing with each other**. Without it:

```python
some_property.host = some_user
# some_user.properties does NOT include some_property — it's a separate,
# unrelated attribute that nobody updated.
```

If some other code in the same request then reads `some_user.properties` — before anything is flushed or committed — it gets a stale answer missing the property you just attached. You'd have to remember to update both sides by hand every time (`some_property.host = some_user` *and* `some_user.properties.append(some_property)`), and forgetting the second one is an easy, quiet bug. The database itself was never at risk either way — only `host_id` actually gets written to Postgres — this is purely about keeping the two Python-side views of one relationship consistent while you're working with them in memory.

**Where the join condition comes from:** neither `relationship()` call above specifies one explicitly. At mapper configuration time (app startup), SQLAlchemy scans the `properties` and `users` tables for a `ForeignKey` connecting them, finds `host_id` is the only one, and uses `properties.host_id = users.id` as the join for both directions — `Property.host` walks it one way, `User.properties` walks it the other. The FK is the single source of truth; `relationship()` just rides on top of it. This resolution happens once and is cached — it's not redone on every `.host` access, only the actual `SELECT` (when needed) is a per-access round-trip.

**Should you spell out the FK explicitly anyway?**

```python
host: Mapped["User"] = relationship(foreign_keys=[host_id], back_populates="properties")
```

Not required here — with exactly one FK between the two tables, there's nothing to disambiguate, so the implicit version above (no `foreign_keys=`) is what this codebase uses. `foreign_keys=` only becomes *necessary* the moment there's more than one FK between the same two tables — e.g. if `Property` ever gained a second column like `last_edited_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))`. At that point SQLAlchemy can no longer guess which column a given `relationship()` means, and raises `AmbiguousForeignKeysError` at startup until you tell it explicitly which FK each relationship should use. It's a loud, immediate failure (not a silent bug), so it's fine to add `foreign_keys=` only when that day comes rather than defensively up front.

## 6. `CheckConstraint` — rules Postgres enforces, not just your app

```python
class Property(Base):
    __table_args__ = (
        CheckConstraint("max_guests > 0", name="max_guests_positive"),
        CheckConstraint("price_per_night_cents >= 0", name="price_non_negative"),
    )
```

`CheckConstraint` takes a raw SQL boolean expression and makes Postgres reject any `INSERT`/`UPDATE` that would violate it — **at the database level**, not in Python.

**Why this matters beyond a FastAPI/Pydantic validator:** a request-body validator only runs for traffic that goes through your API. It does nothing for a raw SQL script, a one-off data fix, a different service writing to the same database, or an app-level bug that skips validation. `CheckConstraint` is checked no matter who or what writes the row — the same reasoning as why `created_at` uses `server_default` instead of a Python-side default (see the client-side vs. server-side defaults section below): the database is the one thing every writer has to go through.

`__table_args__` is where table-level configuration lives — anything that isn't really one column's concern alone. This is the same tuple a composite `Index(...)` would go in (see the `Mapped`/`mapped_column` section above).

**`name="max_guests_positive"`** isn't cosmetic — it's the constraint's identifier in Postgres. Alembic uses it to generate `ALTER TABLE ... DROP CONSTRAINT max_guests_positive` if the rule is ever changed or removed, and it's what shows up in the error when a bad row gets rejected (`violates check constraint "max_guests_positive"`) — far more useful than an anonymous, auto-generated name.

**What `CheckConstraint` *can't* do: rules spanning two tables.** Every example above checks a column against a fixed value or another column *on the same row* — Postgres can evaluate those with nothing but the row being written. But `docs/er-diagram.md` also specifies: a reservation's `guest_count` must not exceed `property.max_guests`. That's a comparison across two different tables (`reservations.guest_count` vs. `properties.max_guests`), and a `CHECK` constraint has no way to reach into a different table — it only ever sees the one row being inserted or updated.

Rules like that move to the **service layer** instead (`app/reservation/service.py`): fetch the `Property`, compare `guest_count` against `property.max_guests` in Python, reject if it's over, *before* the `Reservation` is even constructed. The trade-off is real: unlike a `CheckConstraint`, this rule is only enforced by code paths that go through that service function — a different writer (raw SQL, a bug, another service) could still insert a `Reservation` that violates it. It's the same "app-level vs. DB-level" boundary as the point above, just landing on the other side of it for a reason: the database genuinely cannot express this one.

## 7. `TYPE_CHECKING` and string literals — how `User` and `Property` reference each other without crashing

```python
# app/property/models.py
if TYPE_CHECKING:
    from app.auth.models import User
...
host: Mapped["User"] = relationship(back_populates="properties")
```

```python
# app/auth/models.py
if TYPE_CHECKING:
    from app.property.models import Property
...
properties: Mapped[list["Property"]] = relationship(back_populates="host")
```

**The problem this avoids:** `Property` needs `User` for its `host` relationship's type hint, and `User` needs `Property` for its `properties` relationship's type hint. If both files did a normal top-level import (`from app.property.models import Property` / `from app.auth.models import User`), you'd get a circular import: loading `auth/models.py` triggers loading `property/models.py`, which tries to load `auth/models.py` again — except that module is still mid-way through executing (it hasn't finished defining `User` yet), so the second import fails.

**Two pieces solve it together:**

1. **`if TYPE_CHECKING:`** — `TYPE_CHECKING` is a constant that's always `False` while your program actually runs. The import inside that block never executes at runtime — Python skips it entirely. It exists only for static type checkers (mypy, pyright, your editor), which *do* set `TYPE_CHECKING = True` while analyzing the file, so they can resolve `User`/`Property` and give you real autocomplete and type errors. Zero runtime cost, zero circular-import risk.

2. **The quotes**, in `Mapped["User"]` and `Mapped[list["Property"]]` — these are string literals, not references to the actual classes. Python doesn't evaluate a string at class-definition time, so it doesn't matter that `User`/`Property` were never actually imported at runtime. SQLAlchemy resolves these strings later — once every model module has finished loading, it looks up `"User"` and `"Property"` by name in `Base`'s shared registry (the same `Base.metadata` registry from earlier) and swaps in the real classes. This is another reason having exactly **one shared `Base`** matters: the string lookup only succeeds because both classes registered themselves in the same place.

Without the quotes, `Mapped[User]` would try to evaluate `User` immediately while the class body runs — and fail, since `User` was never actually imported outside the `TYPE_CHECKING` block.

## 8. Nullable columns and the soft-delete pattern

```python
deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

**`| None` in the annotation is what makes the column nullable.** SQLAlchemy reads the annotation to decide `NULL` vs. `NOT NULL`, the same way it reads `str`/`uuid.UUID`/etc. to decide the SQL type. Every other field in `User` and `Property` omits `| None`, so they default to `NOT NULL`; `deleted_at` is the deliberate exception.

**Why `deleted_at` exists instead of actually deleting the row (`DELETE FROM properties ...`):** a `Property` with existing reservations can't just vanish — the field's own comment says as much (*"Never hard-delete a property with reservation history"*). Two concrete reasons:

- **Referential integrity** — once `Reservation` rows exist, they'll hold a foreign key pointing at this `Property`. Actually deleting the row either fails outright (Postgres refuses to leave a dangling FK) or, worse, cascades into deleting reservation history that needs to be kept.
- **History has to stay queryable** — a past guest's receipt, a host's earnings history, an admin's audit trail all need the property to still exist and be readable, even after it's no longer bookable.

**The pattern:** "deleting" a property means setting `deleted_at = now()`, not removing the row. It stays in the table forever. Every query that means "currently listed properties" has to explicitly filter `WHERE deleted_at IS NULL`; anything that means "this property, historically" queries by `id` with no such filter.

## 9. `str, enum.Enum` — how a Python enum becomes a real database column

```python
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"

role: Mapped[UserRole] = mapped_column(
    Enum(UserRole, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
    default=UserRole.USER,
)
```

**Why `UserRole` inherits from both `str` and `enum.Enum`:** inheriting from `enum.Enum` alone gives a normal Python enum — `UserRole.USER` — but it wouldn't behave like a string anywhere one is expected (JSON serialization in a FastAPI response, string comparisons, etc.). Adding `str` as a second base makes every member *also* a real string: `UserRole.USER == "user"` is `True`, and it serializes to `"user"` directly instead of an object repr. This is a common pattern specifically because API responses need to serialize cleanly.

**What SQLAlchemy does with it as a column type:** given `Mapped[UserRole]`, SQLAlchemy generates a Postgres native `ENUM` type restricted to exactly the declared members — not a plain `VARCHAR` that merely happens to hold those strings. That means **Postgres itself rejects an `INSERT` with any other value**, the same database-level enforcement reasoning as `CheckConstraint` above — it doesn't matter whether the write went through your app's validation or not; the database won't allow `role = 'superadmin'` to exist unless the enum type itself is altered first (its own migration).

**The gotcha `values_callable=` fixes — a real bug caught by actually running this against Postgres:** by default, SQLAlchemy's `Enum` type stores the Python enum's **member name**, not its `.value`. Without `values_callable=`, `role: Mapped[UserRole] = mapped_column(default=UserRole.USER)` looks completely reasonable and passes every type check — but the column Postgres actually creates only accepts `'USER'`/`'ADMIN'` (uppercase), never `'user'`/`'admin'`, even though `UserRole` inherits from `str` specifically to *be* the lowercase value everywhere else in Python. You can see the mismatch directly:

```python
[e.value for e in UserRole]        # -> ['user', 'admin']       (what Python code compares against)
User.__table__.c.role.type.enums   # -> ['USER', 'ADMIN']       (what Postgres actually stores, by default)
```

This stays invisible as long as everything only ever goes through the ORM (SQLAlchemy converts consistently in both directions, so the app never notices). It breaks the moment anything assumes the lowercase value directly — a hand-written SQL query, an external tool or analytics dashboard reading the table, a data migration, or (concretely, in this codebase) `Payment`'s partial unique index below, whose `postgresql_where=text("kind = 'charge' ...")` failed outright at table-creation time because `'charge'` isn't a value the column would actually accept. `values_callable=lambda enum_cls: [e.value for e in enum_cls]` tells SQLAlchemy to use `.value` instead of the member name, so the database matches what `docs/er-diagram.md` documents and what the rest of the app already assumes.

## 10. Index vs. constraint in Postgres, then `Payment`'s partial unique index — proven live

**Postgres doesn't have two separate implementations of uniqueness — a "constraint" version and an "index" version. There's one mechanism, and a `UNIQUE` constraint is just one way of asking for it.** Take the most ordinary case, a plain `UniqueConstraint` with nothing fancy about it:

```python
class Demo(Base):
    __table_args__ = (UniqueConstraint("x", name="uq_demo_x"),)
```

Ask Postgres what it actually built to enforce that:

```sql
-- what pg_indexes reports for this table:
uq_demo_x -> CREATE UNIQUE INDEX uq_demo_x ON public.demo_unique_test USING btree (x)
```

Postgres silently created a **unique index** to enforce it — that's not an implementation detail specific to this codebase or SQLAlchemy, it's how Postgres itself works. Every `UNIQUE` constraint (and every primary key) is backed by a unique index under the hood, whether you write it as `UniqueConstraint` or as `Index(..., unique=True)` directly.

That reframes what a "unique index" is: not a lesser, hackier path to uniqueness bolted onto a search structure, but **the exact same enforcement mechanism a `UNIQUE` constraint would use anyway** — declared directly instead of through constraint syntax.

**So why ever declare it as an index instead of a constraint, if they're the same thing underneath?** Because Postgres's `UNIQUE` constraint syntax has no `WHERE` clause at all — trying one is a straight syntax error:

```sql
ALTER TABLE demo_unique_test ADD CONSTRAINT uq_partial UNIQUE (x) WHERE (x > 0)
-- ERROR:  syntax error at or near "WHERE"
```

A **partial unique index** — `CREATE UNIQUE INDEX ... WHERE ...`, which is `Index(..., unique=True, postgresql_where=...)` in SQLAlchemy — is the *only* way Postgres lets you express "unique, but only among rows matching this condition." It isn't a workaround reaching past the constraint system; it's the one form flexible enough to say this, because indexes and constraints are the same underlying feature and only the index syntax happens to expose `WHERE`.

**Where this shows up for real: `Payment`'s partial unique index.**

```python
class Payment(Base):
    __table_args__ = (
        Index(
            "ix_payments_one_succeeded_charge_per_reservation",
            "reservation_id",
            unique=True,
            postgresql_where=text("kind = 'charge' AND status = 'succeeded'"),
        ),
    )
```

The rule: **a reservation can never have more than one successful charge.** This is worth walking through end to end because — unlike everything above — it isn't checking one row in isolation; it's a rule about *how many* rows with certain values can coexist.

**Why not just check-then-insert in the service layer?** *"Query for an existing succeeded charge; if there isn't one, insert."* has a race window: two concurrent requests (a double-click, a retried webhook, a duplicate delivery from the payment provider) can both run that check, both see nothing yet, and both proceed to insert — the guest gets charged twice. This is the identical shape of problem as the reservation double-booking rule in `docs/er-diagram.md` (its `EXCLUDE USING gist` constraint): an app-level check-then-write can't be atomic across two separate requests, but a database constraint is, because Postgres evaluates it as part of the write itself.

**Why not a lock instead (e.g. `SELECT ... FOR UPDATE`)?** That would also close the race — but only if *every* code path that ever inserts a `Payment` remembers to take the lock first. Miss it once, in a webhook handler or a future endpoint or a one-off script, and the protection silently vanishes. It would also mean holding a database lock open across an external payment-provider network call, which risks blocking other operations on that reservation for as long as that call takes. A unique index has neither problem: Postgres enforces it unconditionally on every write, and the check itself is instantaneous. (For the fuller "why" — what a race condition actually is, why an in-process lock can't coordinate across multiple app processes, and why the database is the one thing they all genuinely share — see [`docs/concurrency-basics.md`](concurrency-basics.md).)

**Why partial (`postgresql_where=...`) rather than a plain `unique=True` on `reservation_id`:** a plain unique index would allow only one `Payment` row per reservation, ever — but this table needs several: a declined charge attempt (kept for history), a retried charge that then succeeds, and later a refund. Scoping the index to `kind = 'charge' AND status = 'succeeded'` means only *those* rows must be unique per reservation; failed attempts, pending attempts, and refunds are all unrestricted.

**Proof, not just theory:** creating the real table locally and inserting rows in this order —

1. `kind=charge, status=failed` → succeeds (a declined card)
2. `kind=charge, status=succeeded` → succeeds (the retry that works)
3. `kind=charge, status=succeeded` (again, same reservation) → **rejected**:
   ```
   UniqueViolationError: duplicate key value violates unique constraint
   "ix_payments_one_succeeded_charge_per_reservation"
   ```

Step 3 was rejected by Postgres itself, with no application code checking anything — the same real, write-time enforcement as any other `UNIQUE` constraint, just declared as an index because that's the only form that can be partial.

---

# Part B — talking to the database

Once models exist, this is how you actually read and write rows.

## 11. Round-trip

The same pattern as `fetch()`: send a request, wait, get a response. That wait is real — even on `localhost` it's not instant.

Every SQL statement — `INSERT`, `SELECT`, anything — is one round-trip: Python sends it to Postgres over the network, waits, gets a reply back. If you do 50 inserts one at a time, that's 50 round-trips, 50 waits. This is *why* ORMs try to batch things up instead of sending one statement per line of code — which is exactly what `Session` does (next section).

## 12. Transactions and isolation

A **transaction** is a group of changes that either **all** happen or **none** happen — no in-between state. You open one, do some work, then either `commit` (make it permanent) or `rollback` (throw it all away, as if none of it happened).

The guarantee that matters here is **isolation**: while a transaction is open, only *your* connection can see the changes it's making. Every other connection querying the database still sees the old state, as if nothing happened yet.

**Why that's the point, not a limitation:** say your code inserts a `Reservation`, then inserts its `Payment`, as two separate statements. If someone else queried the database in between those two statements, would you want them to see a reservation that exists with no payment? That's a broken, half-finished state. The transaction hides that "in progress" work entirely — nobody sees it until `commit`, when it all becomes visible **at once**. If the `Payment` insert fails, `rollback()` erases the `Reservation` too, so nobody ever sees the half-done version.

**Why this might feel new if you're used to a local DB:** many simple tools run in **autocommit mode** — every statement commits itself immediately, with no explicit transaction ever left open. It's not that transactions didn't exist; the tool just never gave you a reason to notice one was open.

## 13. You probably already have multiple connections open

A natural question: does this "other connections can't see it yet" thing actually come up, or is it theoretical?

It's already true on your own machine, right now. **DBeaver** holds a connection to Postgres. **The FastAPI app** holds a separate connection to the same Postgres. That's already two connections, from one person, at once — neither sees the other's uncommitted work. Open a `psql` terminal too and that's three.

At real scale this is constant: a live app serving many users means many requests hitting the same database at the same moment — which is exactly the double-booking race condition this whole project exists to solve. It's also *why* connection pooling exists (see `core/README.md`) — a pool holds several connections specifically because several things need to talk to Postgres simultaneously, not one at a time. Teams also often share a staging database for integration testing, though giving each developer their own local database (like we've done with Docker) avoids stepping on each other while coding.

## 14. `Session` — the staging area

`Session` is SQLAlchemy's middleman between your Python objects and Postgres. Think of it like a shopping cart, or a form you haven't submitted yet: things can sit in it, tracked, without anything being sent over the network.

```python
user = User(email="a@b.com", hashed_password="...")
session.add(user)
```

**Nothing has touched Postgres yet.** `add()` just tells the `Session`: "track this object, I intend to save it eventually." It's pure Python bookkeeping, zero round-trips.

## 15. `session.execute(select(...))` — actually querying data

Everything in section 14 is about writing. Reading is a separate call: `Session.execute()`, given a `select(...)` construct, is what runs an actual query and returns rows — the SQLAlchemy 2.0 replacement for the older `session.query(...)` API.

```python
from sqlalchemy import select

result = await session.execute(
    select(Payment).where(Payment.reservation_id == reservation_id)
)
```

Same "build it, then run it" split as everywhere else in the ORM: `select(Payment).where(...)` is a pure-Python query object — no round-trip yet, same as `mapped_column()` config not touching the DB until something actually executes it. `session.execute(...)` is what sends it to Postgres and is the round-trip.

**The `Result` you get back isn't the rows themselves — you extract them, and how depends on what you expect:**

| Method | Use when | Behavior |
|---|---|---|
| `.scalars().all()` | Zero or more rows expected | Returns a list of matched objects (empty list if none) |
| `.scalar_one_or_none()` | Zero or exactly one row expected | Returns the object, or `None`; raises if more than one matched |
| `.scalar_one()` | Exactly one row must exist | Returns the object; raises if zero *or* more than one matched |

This is why the `charge_reservation` example in [`docs/concurrency-basics.md`](concurrency-basics.md) uses `.scalar_one()` for the locked `Reservation` (it must already exist — a missing row there is a bug, not a valid outcome) but `.scalar_one_or_none()` for the existing-succeeded-charge check (not finding one is the normal, expected case).

## 16. `flush()` — hit submit

```python
await session.flush()
```

*Now* SQLAlchemy actually sends the pending `INSERT` (and any other pending changes) to Postgres — the first real round-trip. Postgres processes it and can hand back anything it generated (like a server-side `created_at`).

But it's still not final. Think of it like a server accepting your form and giving you a reference number, without yet officially processing it where everyone else can see it. In database terms: your changes exist inside an open **transaction** — visible to you, invisible to every other connection to the database.

## 17. `commit()` — final confirmation

```python
await session.commit()
```

This is what makes the change **permanent and visible to everyone else** — other connections, other requests, other people querying the same database now see it. Until this point, you could still back out entirely.

## 18. `rollback()` — the undo button

```python
await session.rollback()
```

If anything goes wrong before `commit()`, this discards everything pending in the transaction — as if none of it happened. This is the actual safety net that makes transactions useful: you can do several related changes (e.g. create a `Reservation` *and* a `Payment`), and if the second one fails, `rollback()` guarantees the first one never actually happened either. Nothing half-done gets left behind.

## 19. Client-side vs. server-side defaults

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

## 20. Worked example, line by line

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

## 21. Migrations and transactions — same mechanism, different purpose

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
| `back_populates=` | Names the matching `relationship()` attribute on the other class, keeping both sides in sync in memory |
| FK column vs. `relationship` | `host_id` = raw UUID, free to read, use for filtering; `host` = full object, may cost a round-trip, use for reading data |
| `index=True` | Builds a Postgres index — fast lookups on that column |
| `unique=True` | A constraint — Postgres rejects duplicate values (not about speed) |
| Composite `Index(...)` | Put in `__table_args__` to index multiple columns *together*; `index=True` on each column separately does not do this |
| `CheckConstraint` | A SQL rule enforced by Postgres on every write, regardless of what wrote it — not just app-level validation |
| `TYPE_CHECKING` | `False` at runtime, `True` for type checkers only — lets you import a type for hints without a real (circular) import |
| `Mapped["ClassName"]` (quoted) | A string forward reference — SQLAlchemy resolves it later via `Base`'s shared registry, so the class doesn't need to be imported at runtime |
| `Mapped[X \| None]` | Makes the column nullable; omit `\| None` and it's `NOT NULL` |
| Soft delete | Set a `deleted_at` timestamp instead of removing the row — keeps FK history (e.g. reservations) intact |
| `class X(str, enum.Enum)` | Enum members that are also real strings (clean JSON serialization) and map to a Postgres native `ENUM` column |
| `values_callable=` | Makes `Enum(...)` store the member's `.value` (e.g. `"user"`) instead of SQLAlchemy's default of its `.name` (`"USER"`) |
| `UniqueConstraint` in Postgres | Not a separate mechanism from an index — Postgres backs every `UNIQUE` constraint with a unique index under the hood |
| Partial unique index | `Index(..., unique=True, postgresql_where=...)` — enforces uniqueness only among matching rows; the *only* way to do this, since `UNIQUE` constraint syntax has no `WHERE` |
| Round-trip | Send a message to Postgres, wait, get a reply — like `fetch()` |
| Transaction | A group of changes that all happen or none happen; isolated from other connections until committed |
| Isolation | Other connections can't see your changes until you `commit` |
| DML | Data Manipulation Language — `INSERT`/`UPDATE`/`DELETE`, changes to rows |
| DDL | Data Definition Language — `CREATE`/`ALTER TABLE`, changes to structure; what migrations are made of |
| `Session` | In-memory staging area for pending changes |
| `session.execute(select(...))` | Runs a query, returns a `Result` — the 2.0-style replacement for `session.query(...)` |
| `.scalar_one()` / `.scalar_one_or_none()` / `.scalars().all()` | Extract rows from a `Result`: exactly one (raise if not), one-or-none, or a list |
| `.add(obj)` | Stage an object — no network call |
| `.flush()` | Send pending changes to Postgres — first round-trip, still not permanent |
| `.commit()` | Make it permanent and visible to everyone else |
| `.rollback()` | Discard everything pending — undo |
| `default=` | Python computes the value, before touching the DB |
| `server_default=` | Postgres computes the value, when the row is inserted |
