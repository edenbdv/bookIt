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

At real scale this is constant: a live app serving many users means many requests hitting the same database at the same moment — which is exactly the double-booking race condition this whole project exists to solve (`docs/db/concurrency-basics.md`, `docs/db/exclude-constraint-basics.md`). It's also *why* connection pooling exists (see `app/core/README.md`) — a pool holds several connections specifically because several things need to talk to Postgres simultaneously, not one at a time.

## 4. `COMMIT`/`ROLLBACK` are universal SQL — `add()`/`flush()` are not

Worth being precise about which parts of "how ORMs write data" are actually universal, and which are one tool's design choice.

**`COMMIT` and `ROLLBACK` are real SQL statements** — part of the SQL standard, present in every relational database (Postgres, MySQL, SQLite, Oracle, SQL Server), and available from any raw SQL client. Typing `COMMIT;` directly into `psql` does the exact same thing as SQLAlchemy's `session.commit()` — the ORM method is a thin wrapper around that same universal mechanism, not something it invented.

**Staging changes before writing them — `add()` then `flush()` — is not universal.** Raw SQL has no such concept at all: you write `INSERT INTO ...` and it happens, no separate staging step. This "stage first, write later" shape is a specific architectural pattern called **Unit of Work**. SQLAlchemy's `Session` explicitly modeled this on **Hibernate** (Java's dominant ORM), which has the identical shape — even the same method name, `session.flush()`. But it isn't universal across ORMs: Django's default ORM writes immediately on `.save()` (no separate staging step), and Active Record-style ORMs (Rails, Laravel's Eloquent) work the same way.

So: `COMMIT`/`ROLLBACK` would exist no matter what tool touched the database. `add()`/`flush()` reflect a specific design choice SQLAlchemy shares with some ORMs (notably Hibernate) but not all of them. See `sqlalchemy-basics.md` for how SQLAlchemy's `Session`, `add()`, and `flush()` actually work.

## 5. `LIMIT`/`OFFSET` — pagination, and why a query needs it at all

```sql
SELECT * FROM properties WHERE city = 'Tel Aviv' LIMIT 10 OFFSET 20
```

**`LIMIT`** caps how many rows Postgres returns — at most this many, even if far more matched. **`OFFSET`** skips that many matching rows before it starts collecting. Both are real SQL, universal across databases — nothing ORM-specific about the concept, same as `COMMIT`/`ROLLBACK` above.

**Why bother — what breaks without it:** `SELECT * FROM properties WHERE city = 'Tel Aviv'` with no `LIMIT` returns *every* matching row, all at once — could be 5, could be 50,000. That's three separate problems stacked together:

- **The database does more work than needed.** Postgres reads and prepares every matching row, not just the handful actually going to be shown.
- **The network transfers more than needed.** Every row crosses the wire to the app, even though a UI showing "20 properties per page" only needs 20 of them right now.
- **It's a resource-exhaustion risk on a public endpoint.** `list_filtered` backs a public search (BOOK-29). With no `LIMIT`, one request for a popular city becomes an unbounded query — cheap for an attacker to trigger, expensive for the database to answer, repeatedly, on demand.

`LIMIT`+`OFFSET` is how "page 2 of 20 results per page" gets expressed: `LIMIT 20 OFFSET 20` (skip the first page, take the next 20). In general: `OFFSET = (page_number - 1) * page_size`.

**A real gotcha, not just theory:** `LIMIT`/`OFFSET` only produce a *stable* page-by-page result if the query has a deterministic `ORDER BY`. Without one, Postgres is free to return matching rows in whatever order is internally convenient — which can change between two calls (e.g., after a concurrent write), so "page 2" might reshuffle rows already seen on "page 1," or skip/duplicate one across pages. `list_filtered` orders by `price_per_night_cents, id` — price as the meaningful sort, `id` as a tiebreaker, since price alone isn't unique (two properties can share a price, and an untiebroken sort is exactly as unstable as no `ORDER BY` at all for those tied rows). Exposing other sort choices to callers is BOOK-29's concern, not this repository method's.

**One more limitation worth knowing, not fixing now:** `OFFSET` doesn't skip for free — Postgres still has to walk past every skipped row internally before it can start collecting. `OFFSET 20` is cheap; `OFFSET 50000` genuinely costs more, growing with the offset itself. There's a different technique (keyset/cursor pagination — "give me rows after this specific id/timestamp") that avoids this, but it's more complex and not needed at this project's scale; `LIMIT`/`OFFSET` is the right choice for now.

## 6. `JOIN` — reaching a column that lives on a different table

`ReviewRepository.list_by_property_id` needs to filter reviews by `property_id` — but `reviews` has no `property_id` column at all, only `reservation_id`. `property_id` lives on `reservations`. A `JOIN` is what makes a column on one table reachable while querying another.

Concretely, with tiny sample data:

**`reviews`:**
| id | reservation_id | rating |
|---|---|---|
| R1 | X1 | 5 |
| R2 | X2 | 4 |

**`reservations`:**
| id | property_id |
|---|---|
| X1 | P1 |
| X2 | P2 |

`JOIN reservations ON reviews.reservation_id = reservations.id` lines up rows from both tables wherever that condition holds, producing temporary combined rows — both tables' columns side by side, nothing filtered out yet:

| review.id | review.reservation_id | review.rating | reservation.id | reservation.property_id |
|---|---|---|---|---|
| R1 | X1 | 5 | X1 | P1 |
| R2 | X2 | 4 | X2 | P2 |

*Now* `WHERE reservations.property_id = 'P1'` can run against this combined set — it filters the wider rows, keeping only `R1`. Finally, `SELECT reviews.*` throws away the `reservations.*` columns that only existed to make the filter possible, leaving just the review. The join's two parts answer two different questions: **which** table to bring in (`reservations`), and **how** to match up rows between the two (`reviews.reservation_id = reservations.id`, the `ON` condition). Without it, `property_id` simply isn't reachable anywhere in the query at all.

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
| `LIMIT` | Caps how many rows a query returns |
| `OFFSET` | Skips this many matching rows before collecting — together, "page N" |
| Why paginate | Without it: DB does needless work, more data crosses the network than needed, and a public endpoint becomes an unbounded-query risk |
| `JOIN` | Makes a column on a different table reachable, by lining up matching rows from both — the `SELECT`/`WHERE` still only touch the columns you actually asked for |
| `LIMIT`/`OFFSET` without `ORDER BY` | Not actually stable — row order can shift between calls, breaking "page 2 follows page 1" |
