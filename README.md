# BookIt

A booking platform backend built as a Python/FastAPI modular monolith. See [app/README.md](app/README.md) for the code layout.

## Setup (first time on a machine)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) — runs Postgres, Redis, RabbitMQ (and optionally the app)
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

That's it — you don't need to install Python packages globally, and you don't need to install Postgres/Redis/RabbitMQ natively at all. Everything else is `uv` (app + deps) and `docker compose` (infra), covered below.

## Who runs what, and how

**The infrastructure (Postgres, Redis, RabbitMQ) always runs in Docker, in both workflows below — that never changes.** No native install of any of them, ever.

**What changes between the two workflows is where *the app itself* runs** — either directly on your machine, or also inside Docker as its own container. That choice is what determines how the app addresses the infrastructure:

- App running **inside Docker**, alongside the infra containers → reaches them by container/service name (`db`, `redis`) — Docker's internal network resolves those names, but only for containers on that same network
- App running **on your machine**, outside Docker → can't use those internal names at all. It has to go through the port each container *published* to your host machine — `localhost:5432`, `localhost:6379`, etc.

Same infrastructure, same containers — just two different vantage points for reaching them.

### Workflow A — active development (recommended day-to-day)

Run only the infrastructure in Docker, run the app directly on your machine with `uv`. This gives you fast auto-reload on code changes without rebuilding a container each time.

```bash
docker compose up -d db redis rabbitmq
uv run uvicorn app.main:app --reload
```

The app reads `.env` (see `.env.example`), which points at `localhost` — this works because docker-compose publishes each service's port to your host machine (`5432`, `6379`, `5672`).

### Workflow B — full stack in Docker (matches production)

Run everything, including the app, in containers. Useful for testing the actual `Dockerfile`, or verifying the app works the way it will in CI/production.

```bash
docker compose up --build
```

Here the `app` container does **not** use your local `.env` — [docker-compose.yml](docker-compose.yml) overrides `DATABASE_URL`/`REDIS_URL` with container hostnames (`db`, `redis`) instead of `localhost`, since containers reach each other by service name, not `localhost`.

### Why the difference matters

`localhost` means different things depending on where code is running: on your machine, it's your machine. Inside a container, it's *that container* — not the Postgres container next to it. So:
- App running **on your machine** → talks to infra via `localhost:<published-port>`
- App running **inside Docker** → talks to infra via the service name (`db`, `redis`), which Docker's internal network resolves for you

That's why there are two different sets of connection strings — `.env` for Workflow A, and the `environment:` block in `docker-compose.yml` for Workflow B — rather than one shared config.
