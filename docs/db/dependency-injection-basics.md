# FastAPI dependency injection with `yield` — Depends, sessions, and connections

Split out of `repository-pattern.md` while working on BOOK-26: none of this is actually specific to the repository pattern — it's the general mechanics behind `Depends(get_db)`, and applies equally to `service.py`, `router.py`, or anything else that ever depends on a database session. Kept as its own doc so it can be linked to from wherever it's needed, instead of re-explained each time.

**Prerequisite:** assumes you've read [`sqlalchemy-basics.md`](sqlalchemy-basics.md) for what a `Session` is and tracks.

## 1. `Depends(...)` + `yield` — this part is FastAPI's, not Python's

Plain Python's `yield` only knows how to pause and resume a generator — it has no concept of an HTTP request. The behavior that matters here — "run this up to `yield`, hand the yielded value to a route, wait for the *entire request* to finish, then resume the function" — is entirely **FastAPI's own dependency-injection system**, not something `yield` does on its own.

Concretely, FastAPI treats a `Depends(...)` target differently depending on whether it's an ordinary function or a generator function:

- **Ordinary dependency** (`def get_settings(): return Settings()`) — FastAPI calls it once and uses the return value. Nothing pauses; there's no "after."
- **Generator dependency** (`def get_db(): ... yield session ...`) — FastAPI advances it to the `yield`, injects the yielded value into the route, waits for the route (and everything it calls) to finish, then advances the generator one more step to run whatever comes after `yield`.

FastAPI's own docs call this "dependencies with yield." It's built on top of Python's generator mechanics the same way `async with` is built on top of Python's generic context-manager protocol — Python supplies the pause/resume primitive; FastAPI is the layer that decided "a paused generator is the right shape for setup-then-cleanup-after-a-request."

## 2. Why it's still called `get_db` when it yields a `Session`, not a connection

The name is convention, not a literal description of the return type. `get_db`, `get_session`, `get_async_session` are all names used interchangeably for this exact pattern across FastAPI+SQLAlchemy codebases. What actually comes out of `yield` is an `AsyncSession` — SQLAlchemy's object representing "everything one request needs to talk to the database" (see [`sqlalchemy-basics.md`](sqlalchemy-basics.md) for what a `Session` tracks). "Get the db [access this request needs]" is the intent behind the name; `Session` is the concrete type that fulfills it. Nothing in FastAPI or SQLAlchemy requires the name `get_db` specifically — `get_session` would be equally correct.

## 3. Where the closing actually happens — and whether that's the same as closing a connection

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory() as session:
        yield session
    # nothing written here — but this is where closing happens
```

There's no visible `session.close()` call, because `async with` provides it implicitly: no matter how the block ends — falls off the bottom normally, or an exception blows through it — Python guarantees `session.__aexit__()` (its cleanup) runs.

The only line inside the block is `yield session`. So the actual order of events:

1. `async with` opens the session
2. `yield session` — pauses here, hands the session to FastAPI
3. *(the request runs, using the session)*
4. FastAPI resumes the generator — execution continues from right after `yield session`
5. There's no more code in the block, so the block **ends** — and that ending is exactly what triggers the automatic close

Written out longhand without `async with`, it's equivalent to:

```python
async def get_db():
    session = _session_factory()
    try:
        yield session
    finally:
        await session.close()   # this is what "async with" does for you
```

The `finally` is why `async with` is used instead of a bare `session = ...; yield session`: even if the route raises an exception, the block still ends (via the exception), so cleanup still runs. A bare `yield` with no `with`/`try` would leak the session on any error.

**Is closing the session the same as closing the connection? No.** A `Session` and a raw TCP connection are two different layers. [`core/README.md`](../../app/core/README.md) already covers the pooling picture generically: the engine holds a handful of already-open connections; something *checks one out*, uses it, then *returns* it to the pool rather than closing it. A `Session` is what performs that checkout on your behalf:

- The first time the session needs to run a query, it checks out a connection from the engine's pool
- It reuses that same connection for every query in the session, so multiple queries in one request share one transaction
- When the session **closes**, it **returns** that connection to the pool — the connection itself stays open, ready for the next request to check out

So "closing the session" really means "I'm done borrowing a connection for this request, pool — take it back," not "disconnect from Postgres." The pool only actually closes physical connections when it shrinks itself or the app shuts down entirely.

**Why it still matters, even though the connection survives:** if `get_db()` never closed the session, that connection would stay checked out forever — never returned, unusable by any other request. A pool of 5 connections would be exhausted after 5 requests that never released theirs. The `yield`-then-cleanup shape guarantees "checked out for exactly the duration of one request, whether it succeeded or raised an exception."

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `yield` (plain Python) | Pauses a function, hands back a value, keeps local state alive until resumed — has no idea what an HTTP request is |
| "Dependencies with yield" (FastAPI) | FastAPI's own feature: run up to `yield`, inject the value, wait for the request to finish, then resume — not something `yield` does by itself |
| `get_db()` naming | Convention, not a type description — it yields a `Session`, not a raw connection; `get_session` would be equally valid |
| Closing the session | Triggered implicitly by `async with`'s block ending, not a visible `.close()` call |
| `Session` vs. connection | The session borrows a connection from the pool per query and returns it on close; closing the session ≠ closing the TCP connection to Postgres |
