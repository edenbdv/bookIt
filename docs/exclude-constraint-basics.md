# `EXCLUDE` constraint basics (BOOK-24)

Written as theory before touching anything, same approach as `alembic-basics.md` — updated in place once the actual BOOK-24 work happens, so it stays a real reference afterward.

**Prerequisite:** this assumes you know what an index and a B-tree are, and how GiST differs from B-tree — see [`indexes-basics.md`](indexes-basics.md) if not. Everything below is specific to this constraint and BOOK-24.

## 1. Why not just a "regular" constraint — `CHECK` and `UNIQUE` genuinely can't express this

Per [`indexes-basics.md §2`](indexes-basics.md#2-some-constraints-are-indexes--specifically-the-ones-comparing-rows-to-each-other), Postgres's other constraint types cover exactly two cases, and neither is "overlap":

- **`CHECK` can't see other rows at all.** Its expression only ever validates the row being written, in isolation (`check_out > check_in` compares two columns *of that same row*). There's no way to write `CHECK (no other row has overlapping dates)` — a `CHECK` expression has no access to the rest of the table, period.
- **`UNIQUE` can see other rows, but only for exact equality.** `UNIQUE(property_id, check_in, check_out)` only rejects a new reservation that's an *exact duplicate* of an existing one — same property, same check-in, same check-out. It does nothing for the real case: an existing reservation Sep 1–5 and a new one Sep 3–7 genuinely overlap (sharing Sep 3–5), but they're not equal on any column, so `UNIQUE` lets it straight through.

So the two "regular" constraint types split the space into "validate one row alone" and "detect an exact duplicate" — and BOOK-24's rule is neither. `EXCLUDE` exists specifically because it's the one constraint type that accepts *any* operator for the row-to-row comparison, not just `=` — not a more powerful `UNIQUE` for convenience, but the only mechanism in Postgres capable of expressing "reject if any other row satisfies `&&` with this one."

## 2. `btree_gist` — why it's needed to mix `=` and `&&`

BOOK-24's rule needs two conditions checked *together*, in one index: *same `property_id`* **and** *overlapping dates*. The overlap check (`&&`) is naturally GiST. But plain equality (`=`) on a UUID column is normally a B-tree operation — GiST doesn't come with built-in support for simple equality on ordinary scalar types.

`btree_gist` is a small Postgres extension that adds GiST-compatible versions of the standard B-tree operators (`=`, `<`, `>`, etc.) for ordinary types like UUID, integer, date. With it enabled, `property_id WITH =` becomes something GiST can evaluate — which is what lets both conditions live in the same constraint. Without it, Postgres would reject the constraint definition outright (GiST wouldn't know how to handle the `=` part).

## 3. Range types and the half-open interval `'[)'`

Postgres has a native `daterange` type — a value representing a *span* of dates, not a single date. Critically, each end of a range can be **inclusive or exclusive**, written with bracket notation:
- `[` / `]` = inclusive (that boundary date is *part of* the range)
- `(` / `)` = exclusive (that boundary date is *not* part of the range)

`daterange(check_in, check_out, '[)')` means: `check_in` is included, `check_out` is excluded. That's not an arbitrary choice — it directly encodes what a stay actually means: the guest occupies the property *on the night of* `check_in` through the night before `check_out`; `check_out` itself is a day they're gone by morning, free for someone else.

**Concretely, why this makes back-to-back bookings work:**

```
Reservation A: check_in=Sep 1, check_out=Sep 5  ->  daterange includes nights: 1, 2, 3, 4
Reservation B: check_in=Sep 5, check_out=Sep 8  ->  daterange includes nights: 5, 6, 7
```

A's range excludes Sep 5 (checkout day); B's range includes Sep 5 (check-in day) — but as different *endpoints*, so `&&` (overlap) correctly evaluates to **false**. If `'[]'` (both inclusive) had been used instead, both ranges would claim Sep 5, `&&` would be **true**, and a completely legitimate same-day turnover would be wrongly rejected. This is exactly the acceptance criterion *"adjacent stays are allowed."*

## 4. What `EXCLUDE` actually means, generally

```sql
EXCLUDE USING gist (
    expr1 WITH operator1,
    expr2 WITH operator2
) WHERE (predicate)
```

Reads as: **no two distinct rows may simultaneously satisfy every listed condition.** It's a generalization of `UNIQUE` — in fact `EXCLUDE (id WITH =)` *is* a `UNIQUE` constraint on `id`, just spelled with the more general mechanism. `UNIQUE` only ever means "not equal"; `EXCLUDE` lets you plug in *any* operator with a GiST-compatible meaning — including `&&` (overlaps).

**BOOK-24's actual constraint, piece by piece:**

```sql
EXCLUDE USING gist (
    property_id WITH =,
    daterange(check_in, check_out, '[)') WITH &&
) WHERE (status <> 'cancelled')
```

- `property_id WITH =` — only compare reservations for the *same* property
- `daterange(...) WITH &&` — reject if their stays overlap
- `WHERE (status <> 'cancelled')` — cancelled reservations are excluded from the check entirely, so cancelling a reservation immediately frees its dates for someone else — no separate cleanup step needed, it's just that a cancelled row no longer participates in the comparison at all

Both conditions have to hold *together* for a rejection — two different properties sharing the same dates is completely fine; the same property with non-overlapping dates is fine; only same property **and** overlapping dates gets rejected.

## 5. Why `EXCLUDE` instead of an app-level "is it free?" check

Same reasoning already covered in [`concurrency-basics.md`](concurrency-basics.md) for `Payment`'s double-charge prevention — this is the same class of problem, just with a richer comparison (overlap, not plain equality). An app-level check ("query for conflicting reservations, insert if none found") has the identical race window: two concurrent requests can both see "no conflict" before either commits, and both succeed. A `SELECT ... FOR UPDATE` lock would work but (as covered before) depends on every code path remembering to take it. `EXCLUDE` is enforced by Postgres unconditionally, on every write, regardless of what wrote it — the database itself makes the invalid state impossible to create, rather than merely unlikely.

## 6. Declaring this in SQLAlchemy: `ExcludeConstraint`

```python
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import ExcludeConstraint

__table_args__ = (
    ExcludeConstraint(
        ("property_id", "="),
        (func.daterange(Reservation.check_in, Reservation.check_out, "[)"), "&&"),
        using="gist",
        where=(Reservation.status != "cancelled"),
        name="no_overlapping_reservations",
    ),
)
```

`func.daterange(...)` is needed because `daterange` is a Postgres SQL function — there's no Python-level range object being sent; SQLAlchemy just needs to emit the right SQL expression, the same way `func.now()` is already used for `server_default` elsewhere in these models.

**Important:** `ExcludeConstraint` only declares the constraint — it does **not** enable `btree_gist` itself. That extension has to be turned on separately, in its own migration step, before this constraint can be created (Postgres will error on the `CREATE ... EXCLUDE ...` otherwise).

## 7. The actual plan for BOOK-24, once we start

1. A migration enabling `btree_gist` — `op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")`, with the matching `DROP EXTENSION` in `downgrade()`.
2. Add the `ExcludeConstraint` to `Reservation.__table_args__`.
3. Generate the migration — worth noting autogenerate's support for `ExcludeConstraint` is less reliable than for ordinary tables/columns/indexes (it's a more exotic, Postgres-specific feature), so this migration will need a closer read than migration 1 or 2, possibly hand-adjustment.
4. Apply it, then **prove it live**, same method used for `Payment`'s constraint: insert two overlapping reservations for the same property directly, confirm the second is rejected; insert two back-to-back reservations (checkout day = check-in day), confirm both succeed; insert two overlapping reservations where one is `cancelled`, confirm it's allowed.
5. A short comment in the migration explaining why `EXCLUDE` was chosen over app-level checking (ties back to section 5).

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `btree_gist` | Extension adding GiST support for ordinary B-tree operators (`=`, etc.) on plain scalar types |
| Range type (`daterange`) | A Postgres value representing a span, with independently inclusive/exclusive bounds |
| `'[)'` | Half-open range: start included, end excluded — exactly matches how a stay's checkout day is free for the next guest |
| `EXCLUDE` constraint | Generalizes `UNIQUE` to any GiST-compatible operator — "no two rows may satisfy all these conditions simultaneously" |
| `ExcludeConstraint` (SQLAlchemy) | The Python-side declaration; needs `btree_gist` enabled separately, doesn't enable it itself |
