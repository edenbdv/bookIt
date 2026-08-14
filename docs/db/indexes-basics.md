# Indexes: what they are, and B-tree vs. GiST

General index theory — not specific to any one migration or constraint. Split out from `exclude-constraint-basics.md` since it's background knowledge, not BOOK-24-specific.

## 1. What an index actually is, and the idea behind a B-tree

**The problem an index solves:** without one, finding a row means checking every single row in the table — a **sequential scan**. `SELECT * FROM properties WHERE city = 'Tel Aviv'` with no index reads every row, tests `city = 'Tel Aviv'` on each, one at a time. Fine for 100 rows, unusable for 10 million.

**An index is a separate structure, stored alongside the table, built specifically to avoid that.** It doesn't replace the table — it's an *additional*, deliberately lean structure whose only job is answering "where are the rows with this value" fast. Postgres's default index type, and the one used by every index built so far in this project (`ix_properties_city`, `properties_pkey`, the unique indexes on `payments`/`reviews`), is a **B-tree**.

**The idea of a B-tree, from the ground up:** imagine you had to find a specific number in a sorted list of a million numbers. Checking one at a time is slow. But if you know the list is *sorted*, you can do something much better: look at the middle value. If your target is smaller, you know it can only be in the left half — throw away the right half entirely, without looking at a single value in it. Repeat on the remaining half. Each comparison eliminates roughly half of what's left, so you find any value in about log₂(1,000,000) ≈ 20 comparisons instead of up to a million.

A B-tree is that idea, turned into an actual on-disk structure: a tree where each node holds several sorted values, and following the right branch at each node is exactly that "which half could my target be in" decision, repeated level by level down the tree. That's the whole idea — sorted order plus branching lets you *rule out* huge chunks of data instead of checking each one.

**How this becomes a database index specifically:** a B-tree index builds exactly that tree over the *values of a column* — for `ix_properties_city`, the sorted values are every city that appears in `properties`. But a database index needs one more thing beyond a plain "is this value here": it needs to know *which row* that value came from. So every entry in the tree isn't just the value — it's the value **plus a pointer to that row's physical location** on disk (Postgres calls this a TID: which page, which slot within it). The index itself stores nothing else about the row — no other columns, no full data — just enough to search fast, plus a pointer back to where the real row lives.

So a query like `WHERE city = 'Tel Aviv'` becomes: walk down the tree comparing `'Tel Aviv'` against values at each node, branching the same way you would searching for a number, until you reach the matching leaf entries — then follow each one's pointer straight to the actual row, instead of ever scanning the table sequentially.

## 2. Some constraints are indexes — specifically the ones comparing rows to each other

Not every constraint needs an index — only the ones whose entire job is detecting a conflict *between* rows do. The dividing line:

- **Needs no index at all** — `CHECK`, `NOT NULL`. These only ever ask "is *this* row valid, on its own?" (`max_guests > 0`, `email IS NOT NULL`). Postgres evaluates that boolean expression against the row being written — no comparison against other rows, no lookup structure, nothing to index. Every `CheckConstraint` already used throughout this project's models (`guest_count > 0`, `check_out > check_in`, `rating BETWEEN 1 AND 5`) is exactly this: a plain per-row check.
- **Needs an index** — `UNIQUE`, `PRIMARY KEY`, `EXCLUDE`. Each of these compares a row against *other* rows — but the index type they need depends on *what kind* of comparison, not just the fact that a comparison happens:
  - `UNIQUE`/`PRIMARY KEY` compare rows using `=` — "does an equal value already exist?" That's not a new capability layered onto a B-tree; it's the exact same operation a B-tree already performs on every ordinary lookup. A B-tree can compare rows just fine, as long as the comparison is `<`/`=`/`>`.
  - `EXCLUDE` can compare rows using *any* operator, including ones that aren't reducible to a single linear order — like `&&` (overlaps), used for BOOK-24's overlapping-reservation check. Two date ranges can overlap without either being strictly "before" or "after" the other, so there's no sort order where that reduces to `<`/`=`/`>`. *That's* the actual reason `EXCLUDE` needs GiST — not "it compares rows" (B-tree already does that for `UNIQUE`), but specifically "it compares rows using a relationship B-tree structurally cannot express" (see [`exclude-constraint-basics.md`](exclude-constraint-basics.md)).

For the ones that do need an index: when a table declares `PRIMARY KEY (id)` or `UNIQUE (email)`, Postgres doesn't build a separate "constraint" data structure alongside an index — it builds exactly the same B-tree described above, then adds one rule on top: before inserting a new entry, check whether an equal key already exists in the tree, and reject the write if so. This was proven directly against a real database earlier in this project — creating a table with a plain `UniqueConstraint` and asking Postgres what it actually built showed a `CREATE UNIQUE INDEX` statement, not a separate constraint object. See [`sqlalchemy-basics.md`](sqlalchemy-basics.md#11-index-vs-constraint-in-postgres-then-payments-partial-unique-index--proven-live) for the live proof and the fuller reasoning — including why a `UNIQUE` constraint can never take a `WHERE` clause, but a unique index can, which is exactly why `Payment`'s partial uniqueness rule had to be declared as an index.

So `properties_pkey` (the primary key backing `Property.id`) isn't an abstract rule sitting somewhere separate — it's a real B-tree, structured exactly like `ix_properties_city`, just with "reject duplicates" layered on top of the same lookup structure. `Property`'s `CheckConstraint("max_guests > 0", ...)`, right next to it in the same model, has no such structure — there's nothing to build an index over.

## 3. Why it's actually *multi-level* — not just one sorted list

The "cut the search space in half" idea above describes binary search generally. A real B-tree adds something specific on top of that: it's organized into **multiple levels**, and each node holds **many keys at once**, not just one.

**Why:** the real cost of a database search isn't "how many comparisons" — it's **how many disk pages get read**, since reading a page from disk is far slower than comparing values already in memory. If each tree node held only a single key (a plain binary tree), searching a million rows would take ~20 comparisons, but potentially ~20 separate page reads too — slow, and it gets worse as the table grows.

A B-tree instead packs **many keys into each node** — a node is sized to match a disk page (Postgres pages are 8KB), so one page can hold hundreds of sorted keys plus pointers to the next level down. That means each level of the tree doesn't just eliminate half the remaining values — it eliminates a much larger fraction, since you're comparing against hundreds of values per page, not one. The practical structure: a **root** page, often one or two **internal/branch** levels, then the **leaf** level where the actual value + row-pointer entries live. That's usually only **2–4 levels total**, even over millions of rows — a small, roughly constant number of page reads gets you to any row, regardless of table size. That multi-level, high-fan-out shape is what "B-tree" actually refers to — not simply "sorted data you binary search."

(The "balanced" in B-tree is a related, separate guarantee: every leaf sits at the same depth, so search time doesn't vary depending on which value you're looking for.)

**Worked example — a real three-level `ix_properties_city` search, contrasted against the table's actual storage:**

![A three-level B+Tree for ix_properties_city (root splitting on Netanya, two branch nodes each splitting again, four leaves holding alphabetically ordered city entries) next to the properties table's heap storage, showing the same rows in arbitrary insertion order — with pointer arrows crossing between them to show the sorted index has no relationship to physical row placement.](images/city-index-btree.svg)

Reading the four leaves left to right gives alphabetical order (`Ashdod → Haifa → Netanya → Tel Aviv`) — each branch states exactly which cities live under it, and the `<`/`≥` labels on each arrow are the actual comparison made at that node during a search. The heap below shows the same four rows in plain insertion order, with no relationship to the sorted index above — which is exactly why the pointer arrows cross: a sorted index says nothing about where its rows physically live in the table.

**To be precise about what actually improves here:** the index itself is stored on disk too — same as the table. Nothing about a B-tree avoids disk I/O; walking down the tree still means reading pages, node by node. What improves is the *count* of page reads needed, not whether disk is involved. Once a page is loaded from disk into memory (one I/O), searching *within* it — among however many keys it holds — is essentially free: in-memory comparisons are nanoseconds, a disk read is milliseconds, roughly a million times slower. So the real cost of a search is "how many separate page-fetches," and the whole point of packing many keys into each page is making every single one of those reads eliminate far more of the search space.

**Concretely, with 1,000,000 rows:**

- **Binary tree, 1 key per node:** ~20 levels needed (log₂ of a million). If those 20 nodes live on 20 different pages, finding one row costs **~20 disk reads**.
- **B-tree, ~200 keys per page** (realistic for Postgres's 8KB pages with compact keys like UUIDs): each page holds up to 200 keys and ~201 child pointers. Levels needed: log₂₀₀(1,000,000) ≈ **3 levels**. Root page (1 read) → binary-search *in memory* among its ~200 keys to pick the right child → next level (1 read) → same again → leaf (1 read). **~3 disk reads total.**

Same underlying "eliminate most of what's left" idea as before — but because each page holds ~200 keys instead of 1, each single disk read eliminates ~200x more of the search space.

**One more real-world detail:** Postgres also caches recently-used pages in memory (`shared_buffers`) across queries, so a busy index's upper levels (root, often the first branch level) tend to stay cached — effectively free after the first access. It's mainly the much larger, more varied leaf level that actually hits disk regularly. That caching is an optimization layered on top of the multi-level structure, not a replacement for it — the multi-level shape is what keeps the *worst case* small even with a cold cache.

## 4. It's actually a B**+**Tree — two structural differences that matter

What Postgres (and nearly every real database) calls a "B-tree" index is, precisely, a **B+Tree** — specifically the Lehman & Yao variant. Two differences from a textbook B-tree, both building on section 2:

**Internal (branch) nodes hold keys + child-pointers — but never row-pointers.** Two different kinds of pointer are involved, worth not conflating: a **child-pointer** says "go to this page to continue the search" (how the tree itself is connected, level to level); a **row-pointer** (a TID) says "here's exactly where this value's actual row lives in the table." In a plain B-tree, any node could carry a value+row-pointer pair, so a match could technically be found partway down the tree. In a B+Tree, **row-pointers exist only at the leaf level** — internal nodes hold keys and child-pointers only, existing purely to route the search ("if your target is in this range, go to this child page next"), never to point at an actual row. Since internal nodes don't need to carry row-pointers, they fit *more keys (and child-pointers) per page* — this is exactly what makes the high fan-out from section 2 possible: it's specifically because internal nodes are lean (no row-pointer payload) that a single page can hold hundreds of entries.

**Leaf nodes are linked together, sideways.** Each leaf holds a pointer to its neighboring leaf (Postgres links in both directions). This is what makes range queries and `ORDER BY` cheap: once tree traversal finds the *first* matching leaf, a query like `WHERE city BETWEEN 'Haifa' AND 'Tel Aviv'` doesn't repeat the whole root-to-leaf walk for every match — it just follows the leaf chain sideways from there, no further up/down traversal needed.

**Why Postgres specifically uses Lehman & Yao's variant:** it adds a "high key" (an upper bound) to each page plus that right-link pointer, specifically so the tree can be searched safely **without holding read locks**, even while another transaction is concurrently splitting a page. Same theme as [`concurrency-basics.md`](concurrency-basics.md) — even the index's own internal structure is engineered so concurrent access doesn't require blocking.

## 5. B-tree vs. GiST — a different index framework, not a variant

The B-tree just described has one hard requirement: every value needs a single, linear sort order — "less than / equal / greater than" has to mean something for whatever's being indexed. That covers numbers, text, dates, UUIDs (compared byte-by-byte) — anything linearly orderable.

**GiST** ("Generalized Search Tree") isn't a variant of that — it's a framework for building tree indexes around relationships that *aren't* a single sort order: "does this range overlap that range," "does this shape contain that point," "does this document contain these words." It's still tree-based (nodes prune large chunks of the search space, same spirit as the "throw away half" idea above), but what gets tested at each node is a relationship like *overlaps*, not *greater-than*. GiST is what Postgres's range types (`daterange`) and `EXCLUDE` constraints are built on — see [`exclude-constraint-basics.md`](exclude-constraint-basics.md) for where that's actually used, in BOOK-24.

**Why "overlaps" specifically can't be a sort order — proof, not just assertion.** A real ordering requires **transitivity**: if `A < B` and `B < C`, then necessarily `A < C` — that's what lets a B-tree safely discard whole branches without checking them. "Overlaps" doesn't have that property. Take three meetings:

```
8–10, 9–11, 10–12
```

- 8–10 overlaps 9–11 (they share 9–10)
- 9–11 overlaps 10–12 (they share 10–11)
- 8–10 does **not** overlap 10–12 (10 is the exact shared boundary — one ends exactly as the other begins, no shared time at all)

`A` relates to `B`, `B` relates to `C`, but `A` does not relate to `C` — transitivity fails. That's disqualifying: a relationship that isn't transitive can never be reduced to a single linear order, no matter how the values are arranged, so a B-tree cannot express it at all — not "is slower at it," structurally cannot.

**Sorting by start time doesn't rescue this**, because that's a different, real question — you can absolutely say 8–10 starts before 9–11, and that ordering is perfectly valid (and transitive). But "starts before" only tells you which range begins first, not whether the ranges overlap, which depends on comparing *both* boundaries of *both* ranges together (does A's start fall before B's end, **and** B's start fall before A's end). A single sorted coordinate can't capture a relationship that inherently needs two numbers from each side at once — which is exactly the gap GiST exists to fill.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Sequential scan | Reading every row to find a match — what an index exists to avoid |
| `CHECK`/`NOT NULL` | Only validate one row in isolation — no comparison, no index, ever |
| `UNIQUE`/`PRIMARY KEY` | Compare rows using `=` — a B-tree's native operation, needs nothing extra |
| `EXCLUDE` | Can compare rows using operators B-tree can't express (like `&&`) — needs GiST specifically because of *that*, not because it compares rows at all |
| Index | A separate, lean structure (value + pointer to the row) built to find rows without scanning the whole table |
| B-tree | Sorted, multi-level tree letting you rule out most remaining values at each step — Postgres's default index, needs one linear sort order |
| Multi-level / page | Each B-tree node holds many keys (sized to one disk page), so a handful of levels covers millions of rows in only a few page reads |
| Why fewer levels matters | The real cost is disk reads (page fetches), not comparisons — once a page is in memory, searching within it is ~free |
| `shared_buffers` | Postgres's page cache — keeps hot pages (often an index's upper levels) in memory across queries, on top of the multi-level structure |
| B+Tree | What Postgres's "B-tree" actually is: only leaf nodes hold row-pointers (TIDs); internal nodes hold keys + child-pointers, for routing only |
| Linked leaves | Leaf pages point to their neighbors, so range scans/`ORDER BY` just walk sideways instead of re-traversing the tree |
| Lehman & Yao | The specific B+Tree variant Postgres uses — adds a "high key" + right-links so searches don't need read locks during concurrent page splits |
| GiST | A framework for indexing relationships without a single sort order — overlaps, contains, nearest — not one fixed structure |
