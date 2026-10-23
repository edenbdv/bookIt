# What BookIt chose for its integration tests, and why

The BookIt-specific counterpart to `testing-basics.md`: that page explains the general
concepts (unit vs. integration, fixtures, isolation, async pitfalls); this one records
what this project actually picked for BOOK-61, and when each choice should be revisited.
For how to run the suites, see the "Running the tests" section of the repo `README.md`.

---

## 1. Real Postgres/Redis in throwaway containers (testcontainers), not the dev DB

**Chosen:** `testcontainers` starts a fresh `postgres:16-alpine` and `redis:7-alpine`
per test run — the same images as `docker-compose.yml` — and removes them at the end.

**Not chosen:**
- *The dev DB from `docker compose`* — what the `scripts/verify_*.py` scripts used. Test
  results then depend on whatever dev data happens to be there, and every script needs
  its own hand-written cleanup (which doesn't run if the script crashes halfway).
- *A dedicated `db-test` service in `docker-compose.yml`* — workable, but someone has
  to remember to start it, and it isn't torn down between runs.

**Cost accepted:** Docker must be running, and the first run on a machine pulls images.

**Revisit when:** CI (BOOK-63) can't run Docker-in-Docker — then fall back to CI-level
service containers with the same fixtures pointed at them.

## 2. Two suites: unit tests stay Docker-free

**Chosen:** integration tests live in `tests/integration/`, and every fixture that
needs Docker lives in `tests/integration/conftest.py`. pytest only loads a `conftest.py`
for tests in its own directory and below, so the existing unit tests (`tests/payment/`,
`tests/reservation/`, …) never trigger a container and still run in ~2 seconds.

**Revisit when:** a third category appears (e.g. Kafka end-to-end tests) — give it its
own directory and conftest rather than growing this one.

## 3. Schema from `alembic upgrade head`, not `create_all`

**Chosen:** the session-scoped `postgres_url` fixture runs the real migrations against
the empty container (testing-basics.md §3.3).

**Why it isn't optional here:** the `no_overlapping_reservations` `EXCLUDE` constraint
depends on the `btree_gist` extension, which is enabled *only* in migration
`23ec21d8edac` — no model can declare it. `create_all` would build a schema that can't
hold that constraint, i.e. not the schema production runs. Running migrations also means
the migrations themselves are tested on every run; `test_migrations.py` checks they
reach head and that the extension and the constraint exist.

**Revisit when:** never, as long as anything lives only in migrations.

## 4. Isolation: `TRUNCATE` after every test, not rollback

**Chosen:** an `autouse` fixture, `_truncate_tables`, runs
`TRUNCATE ... RESTART IDENTITY CASCADE` on every table (except `alembic_version`) after
each test.

**Why not rollback-per-test:** BookIt's code commits on its own, deliberately — BOOK-55
commits the PENDING payment *before* calling the provider, and
`create_payment_and_react` commits more than once (BOOK-54). Those commits would end a
wrapping test transaction early, and they're exactly what `test_payment_saga.py` needs
to observe for real (testing-basics.md §3.4).

**Details that matter:**
- `autouse`, so a test can't forget it.
- The table list is read from `pg_tables`, not hardcoded — a table added by a future
  migration is covered without touching the fixture.
- Every test reuses the same emails (`guest@test.com`, …) on a `unique` column, so a
  cleanup failure shows up immediately as a duplicate-key error in the next test.

**Revisit when:** the suite gets slow enough that truncating every table per test
dominates run time — then consider truncating only tables a test touched.

## 5. Re-point the app's cached clients, don't `dependency_overrides`

**Chosen:** `get_settings`, `get_engine`, `get_session_factory` and `get_redis_client`
are all `@lru_cache`d singletons. The fixtures set `DATABASE_URL` / `REDIS_URL` (a real
env var beats `.env` in pydantic-settings) and clear those caches, so the *unmodified*
app code connects to the containers on its own.

**Why:** nothing in the app is swapped out — routes, `get_uow`, `UnitOfWork()` and
background code paths all run exactly as in production. `dependency_overrides` would
only cover what goes through FastAPI's `Depends`, and miss direct `UnitOfWork()` calls
(e.g. the saga tests).

**Revisit when:** a test genuinely needs to replace a dependency's *behavior*, not just
its target — then `dependency_overrides` (or `patch`) for that one test.

## 6. Fresh engine and Redis client per test (event-loop alignment)

**Chosen:** the async test plugin (`anyio`, already used by the unit tests) runs each
test in its own event loop, so every per-test fixture clears the caches *before* the
test (the app builds a fresh engine/client on this loop) and disposes/closes them
*after* (nothing outlives the loop). Same bug class as BOOK-51's consumer crash, fixed
there by `async_task`'s `dispose()` (testing-basics.md §3.5).

`test_health.py`'s second test exists specifically to catch a regression here: it fails
with "attached to a different loop" if a client survives into the next test.

**Revisit when:** per-test engine creation becomes a measurable cost — then switch to
one shared loop for the whole run (both lifetimes per-session instead).

## 7. Fixture layering: `db_session` / `app_db` / `api_client`

Three ways into the same test DB, each for a different job:

| Fixture | Who uses the connection | Used for |
|---|---|---|
| `db_session` | The **test's own** code | Creating test data, reading results back |
| `app_db` | The **app's** code (`UnitOfWork()`, `get_engine()`) | Tests calling services directly |
| `api_client` | The app, via real HTTP (`ASGITransport`) — builds on `app_db`, adds Redis | Tests going through routes |

Reads in assertions use column selects (`helpers.payment_rows`,
`helpers.reservation_status`), never ORM objects already in `db_session` — the identity
map wouldn't see writes made through the app's own sessions.

Redis isn't `autouse`: only tests that ask for `api_client` start the Redis container,
and `api_client` `FLUSHDB`s through the app's own client afterwards (rate-limit counters
and tokens would otherwise leak between tests).

## 8. Which layer each test goes through

Chosen per test by **what the guarantee actually lives in**:

| Layer | When | Examples |
|---|---|---|
| Raw DB inserts | The guarantee is a DB constraint, on its own | `test_no_overlapping_reservations.py` — the last line of defense, independent of service logic |
| Service call + `patch()` | The guarantee is commit order; a crash must be injected at an exact point | `test_payment_saga.py` — crash after charge (BOOK-54), crash mid-provider-call (BOOK-55) |
| HTTP via `api_client` | The guarantee includes error mapping, auth or signatures | `test_reservation_api.py` (409, not 500), `test_reservation_idempotency.py`, `test_payment_webhook.py` (HMAC, 401) |

Where a script called service functions directly, the port went through HTTP when that
adds coverage for free — e.g. the webhook tests send a real signed
`POST /payments/webhook` instead of calling `resolve_payment`, so the
signature check and router are in the loop too.

## 9. The `scripts/verify_*.py` ports

| Script | Ported to | Added beyond the script |
|---|---|---|
| `verify_no_overlapping_reservations.py` | `test_no_overlapping_reservations.py` | Other property allowed; cancelling frees the dates |
| `verify_idempotency_keys.py` | `test_reservation_idempotency.py` | Through HTTP, so each error's status code is checked |
| `verify_saga_commit_split.py` | `test_payment_saga.py` | BOOK-55's crash-mid-provider-call case |
| `verify_webhook_resolution.py` | `test_payment_webhook.py` | Real signed webhook; bad signature → 401 |

BOOK-65 deleted the two payment scripts: they depended on the saga's direct
confirm call, which became an event (`microservices-decisions.md` §11–§16).

Also new: `test_reservation_api.py` (overlap through the API → clean 409),
`test_migrations.py`, `test_health.py`.

`verify_idempotency_keys.py` covers **reservation** idempotency (BOOK-36); payment
idempotency (no double charge) is already unit-tested in
`tests/payment/test_create_payment.py`.

`verify_concurrent_reservations.py` wasn't part of BOOK-61 — it's superseded by
BOOK-62's contract test (§11).

**Revisit:** all five scripts can be deleted once the team is comfortable relying on the
tests — they no longer check anything the suite doesn't.

## 11. BOOK-62: the booking-concurrency contract test

`tests/integration/test_booking_concurrency.py` — the one guarantee that must survive
Epic 11's service split: **N simultaneous bookings for the same property and dates →
exactly one reservation.**

**Choices:**
- **Through `POST /reservations` only** ("contract" = public API, testing-basics.md
  §4.5), so the same test still applies once booking lives in its own service.
- **20 requests, one guest each, a fresh `Idempotency-Key` each** — the test is about
  slot contention, not idempotency replay or one user's rate limit.
- **A shared start gate** (`asyncio.Event`): every request is created first, waits on
  the gate, and all are released together (§4.4). Without it, `gather()` starts them one
  by one.
- **Real DB-level overlap, verified, not assumed.** A temporary probe sampling
  `pg_stat_activity` during a burst saw up to 15 app sessions in Postgres at once (the
  engine pool's 5 + 10 overflow) and up to 14 waiting on the advisory lock together.
  (Sampling gotcha: `pg_stat_activity` is snapshotted per transaction — commit between
  samples, or every sample shows the same frozen picture.)
- **Assert the invariant, never the winner:** exactly one 201, the rest 409, zero 500s,
  one non-cancelled row. A second test cancels the winner through the API and checks a
  new burst again yields exactly one.
- **`.delay()` stubbed** for the "reservation received" email. With no RabbitMQ under
  test, Celery's blocking `.delay()` retries the broker for ~4s and freezes the event
  loop (one booking: 4.29s real vs 0.017s stubbed). Irrelevant to slot contention, so
  the file stubs it with an `autouse` fixture: 40 runs in 59s instead of 310s. The same
  blocking behavior is a real production issue, tracked separately.

**Proven to catch what it claims (sanity checks, run locally, never committed):**

| Protection removed | Result | What it shows |
|---|---|---|
| `no_overlapping_reservations` constraint dropped | 20 × 201 — twenty bookings for the same dates | The test catches double booking |
| Per-property advisory lock removed | Still passed, 10 bursts in a row | See below |

**The lock finding.** The advisory lock (reservation/service.py) isn't what prevents
double booking — the constraint is. It prevents a GiST `EXCLUDE` **deadlock**
(`docs/db/concurrency-basics.md` §4.1), which BOOK-39 hit for real. That deadlock is
timing-dependent ("non-deterministically", per that doc), and 10 bursts without the lock
didn't reproduce it. So: the test reliably guards no-double-booking, but is **not** a
reliable deadlock detector — the lock's justification is BOOK-39's observed deadlock,
not this test.

**Stability:** `pytest-repeat` (dev dependency) runs the file repeatedly in one session —
`uv run pytest tests/integration/test_booking_concurrency.py --count=20` — 40/40
passed.

**Revisit when:** the booking path moves into its own service (Epic 11) — this test
should pass unchanged against it; or if deadlock coverage is ever wanted, try
partially-overlapping ranges in mixed order over many more bursts.

## 10. Ryuk, and the network workaround

testcontainers starts a small `ryuk` container that removes test containers even if
the run is killed. It must be pulled once per machine. When Docker can't pull images
(offline, or the Windows port-exhaustion incident during BOOK-61 that blocked all new
connections), `TESTCONTAINERS_RYUK_DISABLED=true` skips it for that run — containers
are still removed on a normal finish, just not after a hard kill. Kept out of config on
purpose: it's a per-run escape hatch, not a default.

**Port-mapping race (Docker Desktop on WSL2).** On the first run after Docker restarts
(or wakes from a pause), setup often failed with `ConnectionError: Port mapping for
container <id> and port 8080 is not available` -- 8080 is ryuk's port; sometimes 9093,
Kafka's. The containers were up; Docker just hadn't published the mapping yet.
testcontainers (4.15.0, the latest) checks only that the container is `running`, then
reads the mapping once (`DockerClient.port()`) and gives up if it's empty.

Fix: an autouse session fixture in `tests/integration/conftest.py`,
`_retry_port_lookup`, retries that one lookup on that one `ConnectionError` -- up to
~10s (20 × 0.5s). Alternatives weighed:

- *Upgrade testcontainers* -- 4.15.0 is already the latest; nothing to upgrade to.
- *Config / env var* -- none touches the lookup. `RYUK_RECONNECTION_TIMEOUT` is how
  long ryuk waits for its client to reconnect, not how long we wait for its port.
- *Disable ryuk* -- works, but loses cleanup after a killed run. Stays an escape hatch.
- *Retry each fixture's container start* -- doesn't reach ryuk cleanly: ryuk is started
  inside the first container's `start()` under a fixed name, so a second attempt
  clashes with the half-started first one, and a failed Kafka `start()` leaks its
  container. One retry at the lookup covers ryuk, Kafka, Postgres and Redis.

The patch leans on a testcontainers internal (`DockerClient.port`), so recheck it when
upgrading testcontainers -- and drop it if a release adds its own retry.

## Known noise

`InsecureKeyLengthWarning` from PyJWT in API tests: the dev default `secret_key`
(`"dev-secret-change-me"`) is 20 bytes, below the 32 recommended for HS256. Harmless in
tests; a real reminder that every deployed `secret_key` must be long.

---

## Cheat sheet

| Decision | Choice | Revisit when |
|---|---|---|
| Test infrastructure | testcontainers, same images as compose | CI can't run Docker |
| Suite split | `tests/integration/` + its own conftest; unit tests Docker-free | A third test category appears |
| Schema | `alembic upgrade head` | Never, while anything lives only in migrations |
| Isolation | `TRUNCATE` all tables after each test, `autouse`, catalog-driven | Truncating dominates run time |
| Pointing the app at containers | Env vars + clear `lru_cache`s, no `dependency_overrides` | A test must replace a dependency's behavior |
| Async lifetimes | Fresh engine/Redis client per test, disposed after | Per-test creation becomes a real cost |
| Fixtures | `db_session` (test) / `app_db` (app code) / `api_client` (HTTP + Redis) | — |
| Test layer | DB for constraints, service + `patch` for commit order, HTTP for contracts | — |
| Manual scripts | All five superseded (four ported in BOOK-61, concurrent one by BOOK-62) | Delete once trusted |
| Ryuk | On by default; `TESTCONTAINERS_RYUK_DISABLED=true` per run when pulls fail | — |
| Port-mapping race | Retry `DockerClient.port()` ~10s in an autouse fixture | testcontainers upgrade (internal API; may ship its own retry) |
| BOOK-62 concurrency test | 20 guests via API, start gate, invariant asserts, `.delay()` stubbed | Booking moves to its own service — must pass unchanged |
| Deadlock coverage | Not reliably tested; lock justified by BOOK-39's observed deadlock | Deadlock coverage becomes a requirement |
| Stability runs | `pytest-repeat`, `--count=20` in one session | — |
