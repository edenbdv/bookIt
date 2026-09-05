# BookIt

A booking platform backend built as a Python/FastAPI modular monolith. See [app/README.md](app/README.md) for the code layout.

## Setup (first time on a machine)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) — runs Postgres, Redis, RabbitMQ, MailHog (and optionally the app, worker, and beat)
2. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) — manages the Python environment and dependencies:
   ```bash
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. Clone the repo, then from its root:
   ```bash
   uv sync
   cp .env.example .env
   ```
   `uv sync` reads `pyproject.toml` + `uv.lock` and creates `.venv` with the exact dependency versions already locked — nothing to configure. `.env` is your local config (gitignored); the example file has working defaults for the Docker-based infra below.

That's it — you don't need to install Python packages globally, and you don't need to install Postgres/Redis/RabbitMQ/MailHog natively at all. Everything else is `uv` (app + deps) and `docker compose` (infra), covered below.

## Seed data for local development

After a fresh `alembic upgrade head` (schema exists but every table is empty), run:

```bash
uv run python -m app.core.db.seed
```

This inserts a small set of realistic sample data — hosts, guests, properties, reservations (including one cancelled, so the `EXCLUDE` constraint's `WHERE status <> 'cancelled'` behavior is visible), payments, and reviews — so there's something to look at in DBeaver or call through the API without inserting rows by hand.

Safe to run again at any time — every seeded row has a fixed, hardcoded id, so re-running finds the existing rows and skips them instead of creating duplicates.

## Who runs what, and how

**The infrastructure (Postgres, Redis, RabbitMQ, MailHog) always runs in Docker, in both workflows below — that never changes.** No native install of any of them, ever.

**What changes between the two workflows is where the app's own processes (`app`, `worker`, `beat`) run** — directly on your machine, or also inside Docker as their own containers. In practice only `app` benefits from staying on the host (auto-reload); `worker`/`beat` are recommended in Docker either way, since Celery has no `--reload` to gain from the host. That choice is what determines how each process addresses the infrastructure:

- Running **inside Docker**, alongside the infra containers → reaches them by container/service name (`db`, `redis`) — Docker's internal network resolves those names, but only for containers on that same network
- Running **on your machine**, outside Docker → can't use those internal names at all. It has to go through the port each container *published* to your host machine — `localhost:5432`, `localhost:6379`, etc.

Same infrastructure, same containers — just two different vantage points for reaching them.

### Workflow A — active development (recommended day-to-day)

Infra, `worker`, and `beat` in Docker; `app` on your machine with `uv`, for fast auto-reload on code changes without rebuilding a container each time.

```bash
docker compose up -d db redis rabbitmq mailhog worker beat
uv run uvicorn app.main:app --reload
```

The app reads `.env` (see `.env.example`), which points at `localhost` — this works because docker-compose publishes each service's port to your host machine (`5432`, `6379`, `5672`, `1025`/`8025` for MailHog).

**MailHog** (BOOK-45) catches every email the `worker` sends without delivering it anywhere — view them at [http://localhost:8025](http://localhost:8025). See `docs/jobs/email-basics.md`.

**Restarting `worker`/`beat` after a change — two different commands depending on what changed:**

```bash
docker compose restart worker    # code-only change (e.g. a task body, notification/service.py)
docker compose up -d worker      # docker-compose.yml itself changed (env vars, command, ports)
```

`restart` re-runs the container's command against the same config it already has — since the code is bind-mounted, it does pick up edited `.py` files, but it does **not** re-read `docker-compose.yml`. `up -d` compares the running container against the compose file and recreates it if that file changed, which is the only way an updated environment variable (or similar) actually takes effect. Using `restart` after an `environment:` change silently keeps the old value — no error, it just doesn't apply.

### Workflow B — full stack in Docker (matches production)

Run everything, including the app, in containers. Useful for testing the actual `Dockerfile`, or verifying the app works the way it will in CI/production.

```bash
docker compose up --build   # starts every service: app, worker, beat, and all infra
```

Here the `app` container does **not** use your local `.env` — [docker-compose.yml](docker-compose.yml) overrides `DATABASE_URL`/`REDIS_URL` with container hostnames (`db`, `redis`) instead of `localhost`, since containers reach each other by service name, not `localhost`.

### Why the difference matters

`localhost` means different things depending on where code is running: on your machine, it's your machine. Inside a container, it's *that container* — not the Postgres container next to it. So:
- App running **on your machine** → talks to infra via `localhost:<published-port>`
- App running **inside Docker** → talks to infra via the service name (`db`, `redis`), which Docker's internal network resolves for you

That's why there are two different sets of connection strings — `.env` for Workflow A, and the `environment:` block in `docker-compose.yml` for Workflow B — rather than one shared config.
