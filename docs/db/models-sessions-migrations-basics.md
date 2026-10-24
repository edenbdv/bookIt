# Models, sessions, migrations — who does what (BOOK-65)

Written while splitting Payment into its own database, where these three got easy to
mix up. The details of each live elsewhere: models in `sqlalchemy-basics.md`, Alembic in
`alembic-basics.md`. This page is only about **how they relate** — and what moves when.

## 1. Three separate things

```
  MODEL (Python class)           SESSION (connection)              MIGRATION (a file)
  services/payment/src/payment/models.py          PaymentUnitOfWork / UnitOfWork    alembic/versions/xxx.py
  "a payments row looks like"    "talk to THIS database"           "change the DB's shape"
            │                               │                                 │
            │ registers on a catalog        │ engine → a URL                  │ run by `alembic upgrade`
            ▼                               ▼                                 ▼
      Base / PaymentBase              bookit  /  payments               the real tables
       (metadata)                     (which DB)                     (columns, constraints)
```

| | What it is | What it decides | What it does **not** decide |
|---|---|---|---|
| **Model** | A Python class (`class Payment(PaymentBase)`) | How rows look in Python: table name, columns | Which database; whether the table exists |
| **Catalog** (`Base`, `PaymentBase`) | The list of models a `Base` knows (`.metadata`) | Which **migrations** (Alembic setup) know about a table | Which database queries go to |
| **Session** (engine → URL) | A connection + one transaction | Which **database** queries go to | The table's shape |
| **Migration** | A file of explicit steps (`op.create_table(...)`) | The table's real **shape** in the DB | Anything about Python code |

## 2. A query only needs table and column names

When code does `uow.payments.add(payment)`, SQLAlchemy writes:

```sql
INSERT INTO payments (id, reservation_id, kind, amount_cents, status) VALUES (...)
```

That's built from the model's **table name and column names** — nothing else. So the
model does **not** have to describe every constraint in the database:

- The DB can have a foreign key the model doesn't mention → queries work the same; the
  DB keeps enforcing it.
- `relationship()` is a Python-only shortcut (`reservation.payments` runs a `SELECT`);
  removing it changes no SQL anyone else sends.

What *does* break: a model column that doesn't exist in the DB, or a different table
name — then the SQL is wrong.

## 3. Which database? The session, always

```python
async with UnitOfWork() as uow:          # engine → database_url         → `bookit`
    await uow.payments.get_by_id(...)

async with PaymentUnitOfWork() as uow:   # engine → payment_database_url → `payments`
    await uow.payments.get_by_id(...)
```

**The same model class, the same query** — the session decides where it goes. Changing a
model's `Base` moves nothing; changing which unit of work the code uses does.

## 4. The three Alembic commands do very different things

| Command | Looks at the models? | What it does | Safe? |
|---|---|---|---|
| `alembic upgrade head` | ❌ **No** | Runs the migration **files** not yet applied, in order — nothing else | ✅ Only changes what the files say |
| `alembic downgrade -1` | ❌ No | Runs the last applied file's `downgrade()` | ✅ |
| `alembic revision --autogenerate` | ✅ **Yes** | **Compares** the catalog (models) with the DB and **writes a new file** with the differences | ⚠️ Read the file before running it |
| `alembic check` | ✅ Yes | Same comparison, but only **reports** differences | ✅ Read-only |

The key point: **`upgrade` never decides anything** — it does what the files say.
Only `autogenerate` reads the models, and it only *writes a file*; nothing touches the DB
until that file is run by `upgrade`.

## 5. How these play out in Payment's split

| Step | Model / catalog | Session (which DB) | Migrations | Tables in `bookit` |
|---|---|---|---|---|
| Before | On Booking's `Base`, with FK + relationship | Booking's `UnitOfWork` → `bookit` | Booking's | Exist, with FK |
| 2a.1 ✅ | On **`PaymentBase`**, no FK, no relationship | Still Booking's → `bookit` | Booking's no longer sees them | **Unchanged** |
| 2a.2 / 2b ✅ | Same | Still → `bookit` | **Payment's own** create them in `payments` (+ copy rows) | Unchanged |
| 3 — switch ✅ | Same | **`PaymentUnitOfWork` → `payments`** | — | Unchanged, no longer used |
| 4 — contract ✅ | Same | → `payments` | A Booking migration **drops** them | **Dropped** |

(All four steps are done — the contract migration is `8b4228f69ed7`.) Between 2a.1 and
step 4, Booking's `alembic --autogenerate` saw tables in `bookit` that
no model in its catalog describes — and proposes `drop_table(...)` for them. **Delete
those lines** from the generated file; the drop happens on purpose, at step 4, after the
copy.

## Cheat sheet

| Question | Answer |
|---|---|
| Which database does a query go to? | The **session's** — its engine's URL |
| What does a `Base` decide? | Which **migrations** know the table — not which DB |
| Must a model match every DB constraint? | No — only table and column names |
| Does `alembic upgrade head` read the models? | No — it only runs migration files |
| What reads the models? | `--autogenerate` (writes a file) and `check` (reports) |
| When do tables / constraints actually change? | Only when a migration file that says so is run |
