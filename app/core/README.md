# core/

`core/` is the deliberate exception to "package by domain" (see [app/README.md](../README.md)) — it holds only cross-cutting infrastructure with no domain logic of its own: settings (`config.py`), the app factory (`main.py`), DB engine/Redis client setup, health checks, middleware, exception handlers. It's the same idea as Django's own "core" app or DDD's "shared kernel."

The discipline that matters: `core/` must never accumulate domain-specific logic. Booking-specific caching belongs in `booking/service.py`, not `core/`, even if it feels convenient to drop it there. If `core/` starts growing business logic instead of infrastructure, that's a sign something is in the wrong place.

## Why share one DB engine / Redis client instead of one per module

A DB engine or Redis client isn't a single connection — it manages a **pool** of several connections (SQLAlchemy defaults to a handful, tunable based on load). Pooling exists because opening a connection is expensive (TCP + auth handshake), so connections get reused instead of reopened per request. With async FastAPI, a request doesn't block a connection while waiting on I/O either, so a modest shared pool goes a long way.

One shared pool, sized for the whole app, lets connection capacity flow to whichever module actually has load at a given moment. Giving each module (`auth/`, `booking/`, `payment/`) its own separate pool would either split the same total budget into rigid silos — `booking/` starved while `auth/` sits idle — or multiply total connections and risk hitting the database's connection limit for no real benefit. A single shared engine/client, owned by `core/`, avoids both problems.

**What "pooling" actually means:** the pool holds N already-open connections (e.g. 5) sitting ready, instead of 0. Per request:

1. A request needs the DB → it *checks out* one of the N open connections from the pool (no new TCP/auth handshake — it's already open and idle)
2. The request runs its query on that connection
3. When done, the connection is *returned* to the pool (not closed) — available for the next request to check out

If every pooled connection is checked out when a new request arrives, the pool either opens a temporary extra connection beyond N (`max_overflow`) or makes the request wait briefly until one is returned. So pooling means reusing already-open connections instead of paying the open/close cost per request, while still allowing real concurrency up to the pool size.

**Automatic vs. manual:** SQLAlchemy (and redis-py) implements the pooling mechanics itself — checkout, return, overflow, waiting — the moment you create an engine/client. You never manually open or close individual connections. What *is* manual is wiring it correctly: creating the engine exactly **once** at startup (in `core/db.py`), not per-request or per-module, and exposing a FastAPI dependency (`get_db`, using `yield`) so each request checks out a session and it's returned when the request ends. FastAPI itself doesn't pool anything — it just provides the request lifecycle hooks (`yield` dependencies, `lifespan`) that SQLAlchemy's pool plugs into.
