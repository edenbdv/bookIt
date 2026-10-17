# Testing basics, in the amount of depth Epic 10 needs

Deliberately general — mostly no BookIt specifics. Covers the three kinds of test
Epic 10 asks for: unit tests (BOOK-60), integration tests against real infrastructure
(BOOK-61), and a concurrency contract test (BOOK-62). For what this project actually
chose and why, see a future `testing-decisions.md`; for the manual checks these tests
replace, see `scripts/verify_*.py`.

## 0. The mental model, if you remember nothing else

```
 UNIT TEST                         INTEGRATION TEST                 CONCURRENCY TEST

 your function                     your code                        N copies of the same
    │                                 │                             request, at once
 fake / mock deps                  REAL Postgres, Redis...             │ │ │ │
 (no DB, no network)                  │                             REAL DB, separate
    │                              real SQL, constraints,           connections
 "is my logic right?"              transactions, commit order          │
                                      │                             "does the guarantee
                                   "does my logic survive           hold when requests
                                    contact with the real            actually collide?"
                                    infrastructure?"
```

Each level catches a class of bug the level before it *cannot* see. A unit test with a
fake session will never catch a violated constraint — the fake doesn't have one. An
integration test that sends requests one at a time will never catch a race — nothing
ever races.

## 1. Unit tests

**What it is:** one unit (a function, a class) tested in isolation. Every dependency
that talks to the outside world — DB, network, broker, clock — is replaced with a
stand-in. Fast (milliseconds), deterministic, no setup beyond Python.

**What it's good at:** business rules and branching. "A different guest can't cancel
this reservation." "A timeout maps to PENDING, not FAILED." "The refund policy returns
50% inside the 7-day window."

**What it can't see:** anything the stand-in doesn't implement — SQL errors,
constraints, locking, transaction boundaries, what actually got committed.

**Pure functions need no stand-in at all.** A state machine, a refund-policy
calculator, a signature check: input in, output out. These are the cheapest, most
valuable unit tests there are — and a hint that pushing logic into pure functions
makes code easier to test.

## 2. Fakes vs. mocks — two kinds of stand-in

| | Fake | Mock |
|---|---|---|
| What it is | A small working implementation you write (`FakeRepository` returning a preset object, `FakeSession` recording `commit()` calls) | An object from `unittest.mock` (`Mock`, `AsyncMock`, `patch`) that records calls and returns whatever you configure |
| What you assert on | The resulting **state** ("reservation is now CANCELLED") | The **interaction** ("`react_to_payment_outcome` was called once with this payment") |
| Risk | Drifts from the real thing if the real interface changes | Couples the test to *how* the code works, not *what* it does — refactors break it even when behavior is unchanged |

Prefer fakes and state assertions; reach for a mock when the thing you care about
genuinely *is* "was this called" (e.g. "a redelivered webhook must not re-run the
saga").

## 3. Integration tests

**What it is:** your code running against real infrastructure — a real Postgres with
the real schema, real Redis. Slower (seconds), but it's the only level that tests the
assumptions your logic makes about that infrastructure.

**What it catches that unit tests can't:**
- A query that's valid Python but wrong SQL.
- A DB constraint (unique, foreign key, `EXCLUDE`) actually firing — and your code
  handling the resulting error correctly.
- Commit order: was the row really persisted *before* the external call?
- Migrations that don't produce the schema the models expect.

### 3.1 testcontainers

A library that starts a real Docker container (e.g. `postgres:16`) from inside the
test run, gives you its connection URL, and tears it down afterwards. No shared dev
DB, no leftover state between runs, same versions as production. Docker must be
running — locally and in CI.

**This is a separate, throwaway database.** It has nothing to do with the dev DB you
use day to day (the `db` service in `docker-compose.yml`): tests never touch dev data,
and dev data never affects tests. Only integration tests use it — unit tests still use
no DB at all. And it starts **completely empty**: no tables, no extensions, no
constraints. Before any test runs, something has to build the schema from scratch —
which is what §3.3 is about.

### 3.2 pytest fixtures and scope

A **fixture** is a function that prepares something a test needs and hands it over.
The test asks for it **by parameter name** — you never call the fixture yourself:

```python
import pytest

@pytest.fixture
def guest():
    return User(email="guest@test.com")      # setup

def test_guest_can_cancel(guest):            # asks for "guest" by name
    assert guest.email == "guest@test.com"
```

pytest sees the `guest` parameter, finds the fixture with that name, runs it, and passes
in the result.

**Teardown with `yield`.** Code before `yield` runs before the test; code after it runs
after the test — even if the test failed:

```python
@pytest.fixture
def db_session():
    session = make_session()   # before the test
    yield session              # the test runs here
    session.close()            # after the test, pass or fail
```

**`scope` decides how many times a fixture runs:**

| `scope` | Runs | Right for | Example |
|---|---|---|---|
| `"function"` (default) | Once **per test** | Things each test must get **fresh** | Clean DB state, a new session |
| `"session"` | Once **per test run** | Things that are **expensive** to set up | Starting the Postgres container, running migrations |

**Why you need both.** Starting a container and running migrations takes seconds — do
that per test and 50 tests take minutes. But if all tests share the same data, one
test's leftover rows break the next. So combine them — a fixture can ask for another
fixture the same way a test does:

```python
@pytest.fixture(scope="session")
def postgres():
    with PostgresContainer("postgres:16") as pg:   # once for the whole run
        run_migrations(pg.get_connection_url())
        yield pg
                                                    # container removed at the end

@pytest.fixture                                     # scope="function": per test
def clean_db(postgres):                             # depends on the session fixture
    yield postgres
    truncate_all_tables(postgres)                   # clean up after every test

def test_one(clean_db): ...
def test_two(clean_db): ...
```

What actually happens in a run:

```
container up + migrations      (once)
  test_one  → truncate
  test_two  → truncate
container removed              (once)
```

The expensive part is built once, and every test still starts from a clean DB.

**Where fixtures live.** Fixtures in a `conftest.py` file are available to every test
in that directory and below, without importing them — the usual home for shared DB
fixtures.

### 3.3 Build the schema with migrations, not `create_all`

The test container starts empty (§3.1), so the schema has to be built from zero.
Two ways to do that:

| | `Base.metadata.create_all()` | `alembic upgrade head` |
|---|---|---|
| What it does | Walks every model class and issues `CREATE TABLE` straight from the model definitions | Runs every migration in order, first to last (`head` = the latest) |
| Builds | What the **models say** | What **production actually runs** |

**Migrations aren't only for upgrading an old DB.** They're also the recipe for building
a new one from nothing — running them against an empty DB is exactly what happens on a
fresh deployment. So "the code is up to date" doesn't make them unnecessary: the DB
doesn't know about any table until something runs the `CREATE`s.

**"But the models already contain the current schema — isn't that the same thing?"**
Mostly, yes: models and migrations are supposed to describe the same tables, columns
and constraints. But a model can only describe *tables*. A migration can contain things
no model has anywhere to put:

1. **Postgres extensions.** `CREATE EXTENSION` isn't part of any table. BookIt's own
   case: the `EXCLUDE` constraint *is* declared on the model
   (`app/reservation/models.py`), but the `btree_gist` extension it depends on — needed
   to mix plain equality and range overlap in one GiST constraint — is enabled *only*
   in a migration (`23ec21d8edac_enable_btree_gist_extension.py`). `create_all` on an
   empty DB would try to create the constraint without the extension, and fail.
2. **Data migrations.** Backfilling a new column, inserting fixed rows. Models describe
   structure, never rows.
3. **Raw-SQL objects.** Triggers, functions, views written by hand in a migration.

**And even when nothing differs, it still matters.** If a model and its migrations
drift apart — someone changes a model and forgets the migration — `create_all` builds a
DB that matches the model, the tests pass, and production (built from migrations)
breaks. Building the test DB from migrations means the tests run against the schema
that will really exist, and catches that drift as a test failure — which is exactly
when you want it to fail.

**In short:** `create_all` builds what the model *says*; migrations build what
production *runs*. Usually the same — and when they aren't, you want the tests to be
the ones that notice.

### 3.4 Isolation between tests

**The problem.** Every test runs against the same DB — the container is built once
(§3.2). If one test leaves rows behind, the next one starts with them:

```
test_create_reservation   → books Sep 1–5, doesn't clean up
test_list_is_empty        → expects 0 reservations, finds 1  ❌
```

`test_list_is_empty` fails only when it runs *after* the first test; alone, it passes.
Run-order-dependent failures are the most confusing kind, so every test must start
from a known, clean state.

**"But doesn't each test get a fresh session anyway?"** Yes — and that doesn't help.
The word *session* means two unrelated things here:

| | What it is | Created |
|---|---|---|
| pytest `scope="session"` | "The whole test run" | — |
| The DB (container) | The Postgres server, its tables and data | **Once** per run |
| SQLAlchemy `AsyncSession` | A **connection** to that DB — a conversation with it | **Per test** |

A new SQLAlchemy session is a new connection *to the same DB*. Anything a previous test
committed is still sitting in the tables:

```
DB (built once):   reservations: empty

test_one:  new session → INSERT → commit
DB:                reservations: 1 row        ← persisted in the DB itself

test_two:  new session → SELECT → finds that row  ❌
```

So the per-test fixture has two jobs: open a fresh session, **and** clean the data up
afterwards. Two common ways to do the cleanup:

### Option 1 — rollback per test

```
BEGIN          ← fixture opens a transaction
  test runs, inserts rows
ROLLBACK       ← fixture undoes everything; DB is back to how it was
```

Very fast — nothing is ever really written. **But it breaks when the code under test
calls `commit()` itself:**

```
BEGIN                          ← fixture
  create_payment() → commit()  ← the code ends the transaction and really persists
ROLLBACK                       ← nothing left to undo, the rows are already saved
```

The workaround is SAVEPOINTs — turning the code's `commit()`s into nested save points
inside the fixture's outer transaction. It works, but it's fiddly and fragile, and it
means the real commits are no longer what's being tested.

### Option 2 — truncate per test

```
  test runs; the code commits normally, really persisted
TRUNCATE reservations, payments, ... CASCADE   ← fixture empties every table
```

- **`TRUNCATE`** empties a table in one go — much faster than `DELETE`. Structure,
  constraints and extensions all stay.
- **`CASCADE`** also empties tables that reference it via a foreign key. Without it,
  Postgres refuses to truncate a table other tables still point at.

Slightly slower than rollback, much simpler — and the code runs exactly as it does in
production, commits included.

**Why not a brand-new DB per test instead?** Possible, but slow: creating a DB and
running migrations takes seconds; `TRUNCATE` takes milliseconds. Building once and
truncating per test makes every test *behave* as if it got a fresh DB, at a fraction of
the cost.

**Which one fits BookIt:** truncate. The code commits on its own, deliberately — e.g.
`create_payment` commits the PENDING row *before* calling the provider (BOOK-55), and
`create_payment_and_react` commits more than once on purpose. Those commits are part
of what the tests need to verify, so the cleanup strategy must not interfere with
them.

### 3.5 Async gotchas

**What an event loop is.** `async` code doesn't run on its own — the event loop is what
runs every `await` and switches between them. Each `asyncio.run()` creates a new loop
and closes it at the end.

**The problem: async DB connections belong to the loop they were created on.** An async
engine holds a pool of open connections, and each one is tied to a specific loop. Once
that loop closes, using the connection from a different loop fails in confusing ways —
`attached to a different loop`, or a test that just hangs.

**Why tests hit it.** Depending on the async test plugin and its config, each test
often runs in **its own loop** — while the engine and the DB fixture are built **once**
(§3.2):

```
test_one:  loop A  → engine created, connections bound to loop A
           loop A closes
test_two:  loop B  → reuses the same engine, its connections still bound to loop A  ❌
```

**BookIt has already hit this exact bug, outside tests.** `get_engine()` in
`app/core/db/engine.py` is `@lru_cache`d — one engine for the whole process. That's
why BOOK-51's consumer crashed on its second message: a new `asyncio.run()` meant a new
loop, with the cached engine's connections still bound to the old one. The fix was
`async_task`, which `dispose()`s the engine after every `asyncio.run()`. In tests it's
the same problem, with "a new loop per test" in place of "a new `asyncio.run()` per
call" — and the cached `get_engine()` means the app code will reuse one engine across
tests unless the fixtures deal with it.

**Two fixes — pick one, and keep the engine's lifetime and the loop's lifetime
aligned:**

1. **Both per run:** one loop for all tests, one engine. Faster; how to share the loop
   depends on the plugin's config.
2. **Both per test:** a fresh engine per test, `dispose()`d at the end (and the cached
   `get_engine()` cleared, or overridden, so app code doesn't hold on to an old one).
   Slightly slower, simple and safe.

**Plugin note.** BookIt's existing async tests use **`anyio`**'s pytest plugin
(`@pytest.mark.anyio`), not `pytest-asyncio`. The principle above is the same for
both; the config for sharing a loop across tests differs between them, so check the
plugin's own docs rather than copying a `pytest-asyncio` example.

### 3.6 Testing through the API

**What it means.** Instead of calling a service function directly
(`create_payment(uow, ...)`), the test sends a **real HTTP request** — the same one the
frontend would:

```python
response = await client.post(
    "/payments",
    json={"reservation_id": str(rid)},
    headers={"Idempotency-Key": "abc", "Authorization": f"Bearer {token}"},
)
assert response.status_code == 201
```

**`ASGITransport`: no server, no port.** Normally sending HTTP means running a server
(`uvicorn`) and calling `localhost:8000`. `ASGITransport` plugs `httpx` **straight into
the `app` object** inside the same Python process — the request goes through all of
FastAPI, with no network in between:

```python
from httpx import AsyncClient, ASGITransport
from app.main import app

async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
    ...
```

**What one such test covers at once:**

- **Routing** — `/payments` reaches the right function.
- **Validation** — Pydantic rejects a bad body with 422.
- **Auth** — `get_current_user` rejects a missing token with 401.
- **Error handling** — the router's `except` blocks map domain errors to 409, not 500.
- **The DB** — the row was really persisted.

A unit test of `create_payment` covers only the last of these — and without a real DB.

**`dependency_overrides`: swapping a dependency.** Routes get their DB access through
`Depends(...)` — in BookIt, `Depends(get_uow)` (`app/core/db/unit_of_work.py`). A test
can tell FastAPI "use this version instead":

```python
app.dependency_overrides[get_uow] = get_test_uow   # points at the test container
```

**Two BookIt-specific notes:**

1. **An override may not be needed at all.** `UnitOfWork` ends up at `get_engine()`,
   which reads `database_url` from settings. If the fixture points that setting at the
   test container (e.g. via the environment variable) **and clears the `lru_cache`s**
   on `get_settings`, `get_engine` and `get_session_factory`, the whole app connects to
   the container on its own — closer to production, since no code is swapped out.
   Without clearing the caches, an engine already built against the dev DB keeps being
   used (same caching that §3.5 is about).
2. **Redis is in the request path too.** `POST /payments` also runs
   `enforce_user_rate_limit`, which uses Redis. API-level tests therefore need a Redis
   container as well — or an override for that one dependency, if a test isn't about
   rate limiting. That's why BOOK-61 says "Postgres/Redis", not just Postgres.

## 4. Concurrency tests

**What it is:** fire N copies of the same operation **at the same time** against real
infrastructure, and assert the system's guarantee still holds — e.g. "10 simultaneous
bookings for one slot → exactly 1 succeeds, 9 get 409."

### 4.1 Sequential ≠ concurrent

Inserting two overlapping rows one after the other proves the constraint exists. It
does **not** prove the system is safe under a race: the dangerous window is between
"check availability" and "insert", and sequential requests never land inside it.

### 4.2 Each request needs its own connection

`asyncio.gather()` over N requests that all share **one** session/connection is not
concurrent at the DB level — a connection runs one statement at a time, so the
requests silently queue up behind each other and the test passes even when the code is
broken. Each concurrent request must get its own session from the pool (going through
the API with a real session-per-request dependency does this naturally).

### 4.3 Assert on the outcome, not the timing

Don't assert *which* request wins — that's nondeterministic. Assert the invariant:
- exactly one success, all others the expected conflict error;
- exactly one row in the DB afterwards;
- no 500s (an unhandled DB error under contention is a bug, even if no double-booking
  happened).

### 4.4 Races are probabilistic

A race that exists might not show up in one run. Raise the odds: more concurrent
requests, a barrier so they start together, repeat the test a few times. A concurrency
test that passes once proves little; one that passes 20 runs in a row is meaningful.

### 4.5 Why "contract" test

A contract test checks **observable behavior through the public interface** (the API),
not implementation details. "No double booking" is a promise to users, not a property
of one function. Written this way, the same test stays valid through refactors — and
through an architecture change like splitting into services — which is exactly when
you most need it.

## 5. How the three fit together

```
            ▲   fewer, slower, more realistic
           ╱ ╲
          ╱ C ╲      concurrency / contract: a handful, for the guarantees that matter most
         ╱─────╲
        ╱  I    ╲    integration: one per real DB/infra interaction worth trusting
       ╱─────────╲
      ╱    U      ╲  unit: many, fast, for every business rule
     ╱─────────────╲
            ▼   more, faster, more isolated
```

Unit tests tell you the logic is right; integration tests tell you it's still right
against reality; concurrency tests tell you it's still right when reality gets busy.
Most bugs are caught cheaply at the bottom — the expensive ones only at the top.

## Cheat sheet

| Question | Answer |
|---|---|
| Unit or integration? | Does the test need real infrastructure to be meaningful? No → unit. Yes → integration |
| Fake or mock? | Asserting on resulting state → fake. Asserting "was it called" → mock |
| Fixture scope for the DB container | `session` — start once |
| Fixture scope for clean DB state | `function` — fresh per test |
| Which DB do integration tests use? | A throwaway container, separate from dev, starting empty |
| Schema in tests | Real migrations, not `create_all` — models can't hold extensions, data migrations or raw SQL, and drift would go unnoticed |
| Isolation when code commits itself | Truncate per test |
| Async engine in tests | Engine lifetime = loop lifetime (both per run, or both per test with `dispose()`) |
| Does a fresh SQLAlchemy session mean a fresh DB? | No — it's a new connection to the same DB; committed rows are still there |
| Testing through the API | `AsyncClient` + `ASGITransport` — real HTTP into the app, no server; point settings (or `dependency_overrides`) at the test containers |
| Concurrent requests | One connection/session each, or it isn't concurrent |
| What a concurrency test asserts | The invariant (exactly one winner, no 500s), never which one won |
| Why "contract" | Tests promised behavior via the public API, survives refactors |
