# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BookIt — a short-term rental platform backend (Airbnb-style: hosts list properties, guests book stays), built as a Python/FastAPI **modular monolith**. See [docs/db/er-diagram.md](docs/db/er-diagram.md) for the full domain model and its constraints, and [app/README.md](app/README.md) for the module layout philosophy.

## Commands

### Setup (first time)

```bash
uv sync
cp .env.example .env
```

`uv sync` reads `pyproject.toml` + `uv.lock` into `.venv` — no global Python packages needed. Infra (Postgres, Redis, RabbitMQ, MailHog) always runs in Docker, never installed natively.

### Day-to-day development

```bash
docker compose up -d db redis rabbitmq mailhog worker beat   # infra + worker/beat (no --reload to gain from the host)
uv run uvicorn app.main:app --reload                         # app on the host, auto-reload
```

The app reads `.env`, which points at `localhost` (infra ports are published by docker-compose). To instead run the whole stack — app included — in Docker (matches production, uses the `Dockerfile`, no auto-reload-on-save from host edits):

```bash
docker compose up --build
```

Here `docker-compose.yml` overrides `DATABASE_URL`/`REDIS_URL` to use container hostnames (`db`, `redis`) instead of `localhost`, since the app container reaches infra containers by service name, not `localhost`.

After editing `worker`/`beat` code, `docker compose restart worker` picks it up (bind-mounted). After editing `docker-compose.yml` itself (env vars, command), `restart` is not enough — it doesn't re-read that file; use `docker compose up -d worker` to recreate the container. See [README.md](README.md#workflow-a--active-development-recommended-day-to-day) for the full explanation.

### Migrations

```bash
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "message"
```

A model only gets picked up by autogenerate if its module is imported somewhere Alembic loads — that's why [alembic/env.py](alembic/env.py) explicitly imports every module's `models.py`. Adding a new module means adding its import there too, or its tables silently won't be detected.

### Seed data

```bash
uv run python -m app.core.db.seed
```

Idempotent — every seeded row has a fixed id, so re-running finds existing rows instead of duplicating them. Needs `alembic upgrade head` run first.

### Lint / format

```bash
uv run ruff check .
uv run ruff check --fix .
uv run black .
```

Both also run via pre-commit ([.pre-commit-config.yaml](.pre-commit-config.yaml)): `ruff --fix` then `black`.

### Tests

No automated test suite exists yet (planned: BOOK-61/62, testcontainers-based integration tests). The current stand-in for the trickiest piece of logic — the overlapping-reservations `EXCLUDE` constraint — is a manual verification script, not pytest:

```bash
docker compose up -d db   # needs the real dockerized Postgres
uv run python scripts/verify_no_overlapping_reservations.py
```

## Architecture

### Package by feature, not by layer

Each domain module (`app/auth/`, `app/property/`, `app/reservation/`, `app/payment/`, `app/review/`) is a self-contained vertical slice with the identical internal shape:

- `models.py` — SQLAlchemy ORM (the DB table)
- `repository.py` — data access only; inherits `BaseRepository` (`core/db/repository.py`) for generic CRUD (`get_by_id`, `list`, `add`, `delete`), adds only module-specific queries
- `schemas.py` — Pydantic `*Create`/`*Read`/`*Update` classes (the API contract — deliberately **not** the same shape as `models.py`; see [docs/domain-layer/pydantic-schemas-basics.md](docs/domain-layer/pydantic-schemas-basics.md))
- `service.py` — business logic, HTTP-independent
- `router.py` — thin FastAPI `APIRouter`

`notification/` is the one exception: no `models.py`, since it reacts to other modules' events rather than owning a table.

The reason for this shape (over one shared `models/`, `services/`, `routers/` folder) is extraction later: each module can become its own service by moving a folder, not untangling cross-cutting imports (see Epic 11).

### `core/` — cross-cutting only, no domain logic

`core/` holds shared infrastructure and nothing else: settings, DB engine/session, Redis client, the generic repository base, the Unit of Work, the app factory, health checks. The discipline: if `core/` starts accumulating anything module-specific, it's in the wrong place. Full rationale in [app/core/README.md](app/core/README.md).

- **`config.py`** — `Settings` (Pydantic `BaseSettings`, reads `.env`) exposed via `get_settings()`, `@lru_cache`d into a singleton. Every module reads config through this, never `os.environ` directly.
- **`db.py`** — one shared async SQLAlchemy engine/session pool for the whole app (not one per module — see the README for why). `get_db()` is the FastAPI dependency that yields a request-scoped session.
- **`unit_of_work.py`** — `UnitOfWork` is an async context manager wrapping one `Session`: `__aenter__` builds every module's repository against that *same* session (so writes across `uow.properties`, `uow.reservations`, etc. share one transaction), commits on clean exit, rolls back on exception. This is how a service coordinates multi-table writes atomically. See [docs/domain-layer/unit-of-work-basics.md](docs/domain-layer/unit-of-work-basics.md).
- **`repository.py`** — `BaseRepository[ModelT]` is generic CRUD with **no soft-delete awareness**: `delete()` always issues a real `DELETE`. Soft-delete (currently only `Property.deleted_at`) is a domain decision enforced in that module's `service.py`, not here.
- **`models.py`** — `Base(DeclarativeBase)`, the shared metadata catalog every model registers into.

### The domain model and its non-obvious constraints

Full detail in [docs/db/er-diagram.md](docs/db/er-diagram.md); the load-bearing ones to know before touching `reservation/` or `payment/`:

- **No double-booking is enforced at the DB level**, not the app level: a Postgres `EXCLUDE USING gist` constraint on `(property_id, daterange(check_in, check_out, '[)'))` with `WHERE status <> 'cancelled'`. An app-level "is it free?" check can't prevent two concurrent requests both passing the check before either writes — only the DB constraint can. Requires the `btree_gist` extension (see [docs/db/exclude-constraint-basics.md](docs/db/exclude-constraint-basics.md), [docs/db/concurrency-basics.md](docs/db/concurrency-basics.md)).
- `Reservation.total_cents` is a **snapshot** at booking time, never derived from `Property.price_per_night_cents` at read time — a later price change must not alter what a guest already agreed to.
- At most one *successful* charge per reservation, enforced by a partial unique index (`kind='charge' AND status='succeeded'`) — failed/retried payment attempts still get their own rows, kept for the saga/idempotency work (Epic 8).
- Money is always integer cents, never floats. Dates (`check_in`/`check_out`) are `date`, not `datetime` — avoids timezone off-by-one-night bugs. Primary keys are UUIDs, not sequential ints.
- `Property.deleted_at` (soft delete) exists so a delisted property never orphans its reservation history — but `BaseRepository.delete()` won't stop a hard delete; that guard belongs in `property/service.py`.

### Why schemas.py is never models.py

`schemas.py` classes are intentionally a different shape than the SQLAlchemy model for the same entity: no server-generated fields (`id`, `created_at`) in `*Create`, no sensitive columns (`User.hashed_password`) in `*Read`, and validation (e.g. `Review.rating` between 1–5) happens here for a clean `422` before touching the DB. `ConfigDict(from_attributes=True)` + `SomeRead.model_validate(orm_instance)` is what builds a response schema directly from an ORM object's attributes without hand-copying each field. Full writeup: [docs/domain-layer/pydantic-schemas-basics.md](docs/domain-layer/pydantic-schemas-basics.md).

### Docs worth knowing exist

`docs/db/` and `docs/domain-layer/` contain deliberately-written "basics" explainers for each pattern as it's introduced (dependency injection, indexes, SQL fundamentals, the repository pattern, Unit of Work, Pydantic schemas). When working in an area covered there, read the relevant doc first rather than re-deriving the reasoning — they capture *why*, not just *what*.
