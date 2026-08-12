# SQL / transaction basics — not specific to SQLAlchemy

General database concepts that apply no matter what client or language talks to Postgres — raw SQL, `psql`, a different ORM entirely. Split out from `sqlalchemy-basics.md` since none of this is actually about SQLAlchemy's API; that file covers how SQLAlchemy's `Session`/`add()`/`flush()` map onto the reality described here.

## 1. Round-trip

The same pattern as `fetch()`: send a request, wait, get a response. That wait is real — even on `localhost` it's not instant.

Every SQL statement — `INSERT`, `SELECT`, anything — is one round-trip: the client sends it to Postgres over the network, waits, gets a reply back. Do 50 inserts one at a time and that's 50 round-trips, 50 waits. This is *why* ORMs try to batch things up instead of sending one statement per line of code — which is exactly what SQLAlchemy's `Session` does (see `sqlalchemy-basics.md`).

## 2. Transactions and isolation

A **transaction** is a group of changes that either **all** happen or **none** happen — no in-between state. You open one, do some work, then either `COMMIT` (make it permanent) or `ROLLBACK` (throw it all away, as if none of it happened). `COMMIT` and `ROLLBACK` are real SQL statements, part of the SQL standard — not something any particular tool invented.

The guarantee that matters here is **isolation**: while a transaction is open, only *your* connection can see the changes it's making. Every other connection querying the database still sees the old state, as if nothing happened yet.

**Why that's the point, not a limitation:** say some code inserts a `Reservation`, then inserts its `Payment`, as two separate statements. If someone else queried the database in between those two statements, would you want them to see a reservation that exists with no payment? That's a broken, half-finished state. The transaction hides that "in progress" work entirely — nobody sees it until `COMMIT`, when it all becomes visible **at once**. If the `Payment` insert fails, `ROLLBACK` erases the `Reservation` too, so nobody ever sees the half-done version.

**Why this might feel new if you're used to a local DB:** many simple tools run in **autocommit mode** — every statement commits itself immediately, with no explicit transaction ever left open. It's not that transactions didn't exist; the tool just never gave you a reason to notice one was open.

## 3. You probably already have multiple connections open

A natural question: does this "other connections can't see it yet" thing actually come up, or is it theoretical?

It's already true on your own machine, right now. **DBeaver** holds a connection to Postgres. **The FastAPI app** holds a separate connection to the same Postgres. That's already two connections, from one person, at once — neither sees the other's uncommitted work. Open a `psql` terminal too and that's three.

At real scale this is constant: a live app serving many users means many requests hitting the same database at the same moment — which is exactly the double-booking race condition this whole project exists to solve (`docs/concurrency-basics.md`, `docs/exclude-constraint-basics.md`). It's also *why* connection pooling exists (see `app/core/README.md`) — a pool holds several connections specifically because several things need to talk to Postgres simultaneously, not one at a time.

## 4. `COMMIT`/`ROLLBACK` are universal SQL — `add()`/`flush()` are not

Worth being precise about which parts of "how ORMs write data" are actually universal, and which are one tool's design choice.

**`COMMIT` and `ROLLBACK` are real SQL statements** — part of the SQL standard, present in every relational database (Postgres, MySQL, SQLite, Oracle, SQL Server), and available from any raw SQL client. Typing `COMMIT;` directly into `psql` does the exact same thing as SQLAlchemy's `session.commit()` — the ORM method is a thin wrapper around that same universal mechanism, not something it invented.

**Staging changes before writing them — `add()` then `flush()` — is not universal.** Raw SQL has no such concept at all: you write `INSERT INTO ...` and it happens, no separate staging step. This "stage first, write later" shape is a specific architectural pattern called **Unit of Work**. SQLAlchemy's `Session` explicitly modeled this on **Hibernate** (Java's dominant ORM), which has the identical shape — even the same method name, `session.flush()`. But it isn't universal across ORMs: Django's default ORM writes immediately on `.save()` (no separate staging step), and Active Record-style ORMs (Rails, Laravel's Eloquent) work the same way.

So: `COMMIT`/`ROLLBACK` would exist no matter what tool touched the database. `add()`/`flush()` reflect a specific design choice SQLAlchemy shares with some ORMs (notably Hibernate) but not all of them. See `sqlalchemy-basics.md` for how SQLAlchemy's `Session`, `add()`, and `flush()` actually work.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Round-trip | Send a message to Postgres, wait, get a reply — like `fetch()` |
| Transaction | A group of changes that all happen or none happen; isolated from other connections until committed |
| Isolation | Other connections can't see your changes until you `COMMIT` |
| Autocommit mode | Every statement commits itself immediately — no transaction is ever left open to notice |
| `COMMIT`/`ROLLBACK` | Real SQL statements, part of the SQL standard — not ORM-specific |
| Unit of Work | The stage-then-flush pattern (`add()`/`flush()`) — SQLAlchemy and Hibernate use it; Django/Active Record-style ORMs don't |
