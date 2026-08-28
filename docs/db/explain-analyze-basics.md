# Reading `EXPLAIN (ANALYZE, BUFFERS)`

How BOOK-88's numbers were actually produced (`scripts/explain_list_filtered.py`), and how to read the output. General Postgres knowledge, not specific to one query — but every example here is the real `list_filtered` plan, not a toy.

## Glossary — terms exactly as they appear in the output

Keep this open next to any real plan. Look up the word you're staring at; the rest of the doc explains the *reasoning*, this is just the dictionary.

| What you'll see | What it means |
|---|---|
| `Seq Scan on <table>` | Read every row in the table, one by one — no index used |
| `Index Scan` / `Bitmap Index Scan` | Used an index to jump straight to matching rows instead of reading everything |
| `Hash Join` | Built an in-memory lookup table from one side, then matched the other side against it |
| `Hash Cond: (...)` | The condition used to match rows in a `Hash Join` |
| `Nested Loop` | For every row on one side, did a fresh lookup on the other side |
| `Aggregate` / `GroupAggregate` | Combined many rows into one (or a few) result rows — e.g. `avg()`, `count()`, `sum()` |
| `Sort` | An explicit sorting step |
| `Filter: (...)` | The condition applied to decide which rows survive this step |
| `Rows Removed by Filter: N` | This step read N rows that got thrown away because they didn't match the filter |
| `cost=X..Y` | Postgres's *estimate* of this step's cost (startup..total) — **unitless**, not milliseconds or anything physical, only meaningful *relative to another cost number* (no `ANALYZE` needed to see this) |
| `actual time=X..Y` | The *real* milliseconds this step took (startup..total) — only appears with `ANALYZE`, and is **cumulative**: it already includes everything its child steps took |
| `rows=N` | How many rows: the estimate (inside the `cost=` parentheses) or the real count (inside the `actual time=` parentheses) |
| `loops=N` | How many times this step ran (e.g. once per outer row in a `Nested Loop`) — if `N > 1`, the printed `actual time` is *per loop*, not the total |
| `Buffers: shared hit=N` | N pages were already sitting in Postgres's own RAM cache (`shared_buffers`) — zero disk involved |
| `Buffers: shared read=N` | N pages had to be fetched from disk (or the OS's own cache) — not found in `shared_buffers` |
| `Buffers: temp read=N written=N` | N pages were written to / read back from a temporary disk file — this step didn't fit in `work_mem` |
| `Planning:` / `Planning Time` | Time spent *before* execution, deciding how to run the query — separate from the actual run |
| `Execution Time` | Total real time to actually run the query, after planning was already done |
| `InitPlan` | A subquery computed once, up front, whose result is reused by reference wherever needed — not recomputed per row |
| `Materialize` | Buffers a subquery's result in memory so a repeated inner scan doesn't recompute it from scratch |
| `Limit` | Keeps only the first N rows of whatever its child produced |

## TL;DR

- **`EXPLAIN`** — shows the execution plan Postgres *plans* to use.
- **`ANALYZE`** — actually runs the query and shows what really happened: real timings, real row counts.
- **`BUFFERS`** — shows how much data came from the buffer cache vs. had to be read from disk. Useful for telling apart a CPU/plan problem from an I/O problem.

What to look for in the output:

- Is it doing a `Seq Scan` where an `Index Scan` would help?
- Did the planner estimate the row count badly (estimated vs. actual rows way off)?
- Is there an expensive `Nested Loop`?
- Was the data already in memory (`shared hit`) or read from disk (`read`)?
- Where, specifically, is most of the execution time going?

The rest of this doc is the same thing in more depth, with the real BOOK-88 plan as the worked example.

## 0. A minimal example, run directly against the dev DB

No script needed for a one-off check — open a SQL client against the dockerized dev Postgres (DBeaver, `psql`, whatever's already connected) and just run it. In DBeaver: **SQL Editor → New SQL Script** (`Ctrl+]`) against the `bookit` connection, paste, run with `Ctrl+Enter`. `scripts/explain_list_filtered.py` (§5) exists only because that query is ORM-generated and needed capturing rather than hand-typing — most queries don't need a script at all.

The simplest possible case — one table, no joins — run against this project's real seeded `reviews` table:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM reviews WHERE rating = 5;
```

Real output from this project's data (50,006 seeded reviews):

```
Seq Scan on reviews  (cost=0.00..1445.08 rows=9900 width=97) (actual time=0.009..38.973 rows=9931 loops=1)
  Filter: (rating = 5)
  Rows Removed by Filter: 40075
  Buffers: shared hit=820
Planning:
  Buffers: shared hit=121
Planning Time: 0.640 ms
Execution Time: 39.486 ms
```

Reading it with §3/§4's vocabulary, on the simplest case there is:

- **One node, `Seq Scan on reviews`** — no index on `rating`, so Postgres reads every row and tests each one. `Filter: (rating = 5)` is that test; `Rows Removed by Filter: 40075` is how many didn't match (40,075 discarded + 9,931 kept = 50,006, the whole table — confirms nothing narrowed the scan before it started).
- **`Buffers: shared hit=820`, no `read` at all** — every page came from `shared_buffers`, zero disk I/O. `reviews` is a "hot table" (`indexes-basics.md`) at this data size: small enough to live in RAM entirely, so even a full sequential scan never touches disk.
- **`Planning Time: 0.640 ms` vs. `Execution Time: 39.486 ms`** — a distinction worth naming: *planning* is Postgres deciding *how* to run the query (which is cheap here — one table, no choices to make); *execution* is actually doing it. For a complex multi-join query, planning itself can take real time too; here it's negligible next to the scan.
- **No index helped, because none exists on `rating`.** This is also a preview of what an index *would* fix — see `indexes-basics.md` for why a B-tree on a low-cardinality column like this (`1..5`) wouldn't necessarily help much anyway, since it can't rule out large fractions of the table the way it does for a highly selective column.

## 1. Three separate things, stacked

- **`EXPLAIN`** alone: "what plan would you use, and what do you *estimate* it costs?" Doesn't run the query — just shows the planner's guess, based on table statistics (row counts, value distributions) collected by `ANALYZE`/autovacuum. Numbers here (`cost=X..Y`) are **unitless** — not milliseconds, not any physical quantity, only meaningful when compared against another cost number.
- **`ANALYZE`** (a modifier of `EXPLAIN`, unrelated to the `ANALYZE` that updates table statistics — same word, two different meanings): actually **executes** the query and reports *real* `actual time=...` and `rows=...` next to each node's estimate. This is the only way to get real timings instead of guesses.
  - **Caveat that matters:** for `INSERT`/`UPDATE`/`DELETE`, `ANALYZE` really performs the write. Wrap it in a transaction you roll back, or only run it on a `SELECT` (what BOOK-88 needed anyway).
- **`BUFFERS`**: adds where each node's data actually came from — `shared hit=N` (found in Postgres's own page cache, `shared_buffers` — see `indexes-basics.md`'s "hot table" section) vs `shared read=N` (had to fetch from disk/OS cache) vs `temp read/written=N` (spilled to an on-disk temp file because the operation — usually a sort or hash — didn't fit in `work_mem`).

All three together is the only way to answer "where does the time actually go" with numbers instead of guesses — which is exactly the criterion `redis-basics.md` §1 and `cache-aside-basics.md` need before deciding anything is worth caching.

## 2. Reading the tree: bottom-up, inside-out

Indentation is nesting. **Execution order is the opposite of reading order**: the most-indented (innermost) node runs *first*, and its output feeds *up* into its parent, up to the root at the top. Reading top-to-bottom shows you the *result shape* being assembled, not the order work happened in.

A trimmed real example from BOOK-88's plan:

```
Limit                                    <- 5. take first 20, after sorting
  -> Sort                                <- 4. sort by score, then price, then id
       -> Hash Left Join                 <- 3. attach each property's stats
            -> Seq Scan on properties    <- 1a. read all non-deleted properties
            -> Hash                      <- 1b. build a hash table from...
                 -> Subquery Scan anon_1 <- 2. ...the review-stats subquery's output
```

Reading it as "what ran": `properties` gets scanned and the review-stats subquery gets computed (both can start independently — 1a and 1b/2 aren't nested inside each other, they're siblings feeding the same join), then the `Hash Left Join` combines them, then everything gets `Sort`ed, then `Limit` keeps the top 20.

## 3. Estimate vs. actual — the number that catches planner mistakes

Every node shows both:

```
Seq Scan on reservations  (cost=0.00..1215.06 rows=50006 width=32) (actual time=0.004..4.353 rows=50006 loops=1)
```

- `cost=0.00..1215.06` — the planner's *estimate*: startup cost..total cost, unitless (not milliseconds, not anything physical).
- `rows=50006` (in the cost parens) — the planner's *estimated* row count.
- `actual time=0.004..4.353` — *real* milliseconds: startup..total, this execution.
- `rows=50006` (in the actual parens) — the *real* row count returned.
- `loops=1` — how many times this node executed. A node under a `Nested Loop` can run once *per outer row* — multiply `actual time` by `loops` to get that node's real total contribution, since the printed time is *per loop*.

**When estimated and actual rows diverge sharply, the planner's statistics are stale or the query defeats its assumptions** (e.g. correlated columns) — worth knowing even when, like BOOK-88, the estimates were already close enough not to matter.

## 4. The node types that showed up in `list_filtered`'s real plan

| Node | What it means | Where it appeared |
|---|---|---|
| `Seq Scan` | Read every row in the table, in physical order — no index used | Full scans of `reviews`/`reservations` for the two review-aggregation subqueries |
| `Bitmap Index Scan` / `Index Scan` | Used a B-tree/GiST index to jump straight to matching rows | The availability check, using the `EXCLUDE` constraint's GiST index (`no_overlapping_reservations`) |
| `Hash Join` | Build a hash table from one side (usually the smaller), probe it with the other | Joining `reviews` to `reservations` for the stats subquery |
| `Nested Loop` (+ `Anti Join`) | For each outer row, do a fresh (often indexed) lookup on the inner side. `Anti Join` = keep outer rows with **no** match — exactly "no overlapping reservation" | The availability filter once `city`/`guests` had already narrowed `properties` down |
| `GroupAggregate` | Aggregate rows that are already sorted by the group key, streaming — requires a `Sort` immediately underneath it | `_review_stats_subquery()`'s per-property rollup |
| `Sort` | Explicit sort step. Watch `Sort Method`: `quicksort`/`top-N heapsort` (in memory) vs. **`external merge`** (spilled to disk — see `Buffers: temp read/written`) | The `GroupAggregate`'s sort spilled to disk in BOOK-88's plan; the final `ORDER BY` for the `Limit` didn't |
| `Materialize` | Buffer a subquery's output in memory so a repeated inner scan (under a `Nested Loop`) doesn't recompute it from scratch each time | Reusing the review-stats subquery's result across the outer join's rows |
| `InitPlan` | A subquery computed *once*, up front, whose result (`$0`, `$1`, ...) gets reused everywhere it's referenced — not once per row | `_global_mean_rating()`, referenced by every row's Bayesian-score calculation |

## 5. Why this mattered for BOOK-88 specifically

The headline finding — that four differently-filtered searches all took ~70-80ms regardless of how many rows they actually returned — was only visible *because* `ANALYZE` gave real per-node timings. `EXPLAIN` alone (estimates only) would have shown the same query *shape* every time and given no way to see that the cost was concentrated in two InitPlan/subquery nodes that don't shrink no matter how selective the filters are. `BUFFERS` is what pinned the disk-spilling sort down to a specific node (`temp read=283 written=284`) instead of just knowing "it's kind of slow."

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `EXPLAIN` | Shows the planner's *estimated* plan and cost — doesn't execute anything |
| `EXPLAIN ANALYZE` | Actually executes the query, adds *real* timings/row counts next to the estimates. Real writes for INSERT/UPDATE/DELETE — wrap in a rollback |
| `BUFFERS` | Adds `shared hit` (RAM cache) vs `shared read` (disk) vs `temp read/written` (spilled to disk, usually a sort/hash that outgrew `work_mem`) per node |
| Reading order | Indentation = nesting; the most-indented (innermost) node runs *first*, feeding upward to the root at the top |
| `cost=X..Y` | Planner's estimate: startup cost..total cost, unitless — not milliseconds, not any physical quantity |
| `actual time=X..Y` | Real milliseconds: startup..total, for *this* execution |
| `loops=N` | This node ran N times (e.g. once per outer row in a Nested Loop) — `actual time` is *per loop*, multiply for the real total |
| `Seq Scan` | Full table scan, no index |
| `Index Scan` / `Bitmap Index Scan` | Used an index to avoid scanning every row |
| `Hash Join` | Build a hash table from one side, probe with the other |
| `Nested Loop` (`Anti Join`) | Per-outer-row lookup on the inner side; Anti Join keeps rows with *no* match |
| `GroupAggregate` | Streaming aggregation over pre-sorted input — always has a `Sort` right beneath it |
| `Sort Method: external merge` | The sort spilled to a disk temp file — didn't fit in `work_mem` |
| `Materialize` | Buffers a subquery's result so a repeated inner scan doesn't recompute it |
| `InitPlan` | A subquery computed once up front and reused by reference, not once per row |
