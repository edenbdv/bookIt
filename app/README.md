# app/ layout

Modular monolith: each domain module is a self-contained package with the same internal shape, so any of them can later be extracted into its own service without restructuring.

## Modules

BookIt is a short-term rental platform — see [docs/er-diagram.md](../docs/er-diagram.md) for the domain model. One module per entity:

- **auth/** — `User`. Signup/login, password hashing, JWT issuing, `get_current_user` dependency, role-based access control
- **property/** — `Property`. A place a host rents out; CRUD, search, filtering
- **reservation/** — `Reservation`. The core domain: availability, booking creation, concurrency control, state machine
- **payment/** — `Payment`. Mock payment provider, idempotent payment endpoint, saga compensation on failure
- **review/** — `Review`. Guest reviews a property, host reviews a guest
- **notification/** — no models; sends confirmation emails, eventually a pure event consumer
- **core/** — cross-cutting concerns shared by all modules, not a domain module itself; see [core/README.md](core/README.md) for what belongs there and why

## File shape (per module)

- **models.py** — SQLAlchemy ORM models (DB tables)
- **schemas.py** — Pydantic request/response schemas (API contract, separate from DB models)
- **service.py** — business logic, calls repositories, independent of HTTP
- **router.py** — FastAPI `APIRouter`, thin — parses requests, calls `service.py`, returns responses

**Exception: `notification/` has no `models.py`.** Every other module owns a table in [docs/er-diagram.md](../docs/er-diagram.md); `notification/` doesn't — it reacts to events from other modules (a reservation confirmed, a payment failed) rather than storing anything of its own, so there's no model to define. If it ever needs its own persisted state (e.g. tracking which notifications were already sent, for idempotency), add `models.py` back then — don't keep an empty file around just for shape consistency.

The remaining `schemas.py`/`service.py`/`router.py` files are currently empty placeholders; they get filled in as their corresponding Jira epics are implemented.

## Why organize by domain, not by layer

This is **"package by feature"**: each module is a self-contained slice (its own models, schemas, service, router), instead of **"package by layer"** — a single `models/`, `services/`, `routers/` folder each holding every domain mixed together.

Package-by-layer looks simpler at first, but a single feature's code ends up scattered across three unrelated folders, and there's nothing stopping modules from reaching into each other's internals. Package-by-feature keeps each domain's code in one place, makes boundaries explicit, and means extracting a module into its own service later (see Epic 11 in the roadmap) is a matter of moving a folder, not untangling one.

This mirrors how most non-trivial backend codebases are organized in practice (Django apps, Rails "packwerk" modules, DDD bounded contexts) — package-by-layer is mostly seen in small tutorials/toy projects that never need to scale past a handful of features.

For why `core/` is exempt from this, and how shared infrastructure like the DB engine and Redis client work, see [core/README.md](core/README.md).
