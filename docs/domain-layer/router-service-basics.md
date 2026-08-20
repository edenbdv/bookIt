# Router & service basics (BOOK-29)

Written alongside the actual BOOK-29 work — `app/property/router.py` and `app/property/service.py` already have one real endpoint (`POST /properties`) by the time this doc exists, so the examples below are the real code, not a sketch.

**Prerequisite:** this assumes [`repository-pattern.md`](repository-pattern.md), [`unit-of-work-basics.md`](unit-of-work-basics.md), and [`pydantic-schemas-basics.md`](pydantic-schemas-basics.md) — this is the layer that finally *uses* all three together.

## 1. The four layers, now all in place

| Layer | File | Job | Speaks in terms of |
|---|---|---|---|
| HTTP | `router.py` | Parse the request, call `service.py`, shape the response | Pydantic schemas (`PropertyCreate`, `PropertyRead`) |
| Business logic | `service.py` | Orchestrate repositories via a `UnitOfWork`, apply business rules | ORM model instances (`Property`) |
| Data access | `repository.py` | Read/write rows for one module's models | `Session`, `select()` |
| Persistence | `models.py` | Table/column definitions | SQLAlchemy `Mapped[...]` |

Every ticket since `BOOK-26` built one row of this table. `BOOK-29` is the first time all four are wired together end to end for a real request.

## 2. `router.py`'s job — and what it deliberately doesn't do

```python
@router.post("", response_model=PropertyRead, status_code=status.HTTP_201_CREATED)
async def create_property(
    data: PropertyCreate,
    host_id: uuid.UUID,
    uow: UnitOfWork = Depends(get_uow),
) -> Property:
    return await service.create_property(uow, data, host_id)
```

This function's entire job is HTTP-shaped concerns: which schema parses the body (`PropertyCreate`), where `host_id` comes from (query string), what status code a successful create returns (`201`), which schema shapes the response (`PropertyRead`, via `response_model`). It has exactly one line of actual work — call `service.create_property(...)` — and returns whatever that gives back.

**Why `status_code=status.HTTP_201_CREATED` is explicit here, but `GET /{property_id}` (below) doesn't set one at all:** FastAPI's default `status_code` for *every* route, regardless of HTTP method, is `200`. For `GET`, `200` already happens to be the RESTfully-correct answer — so there's nothing to override, and the route just omits the argument. For `POST`, the correct answer for "a resource was successfully created" is `201`, not `200` — so it has to be stated explicitly, or a successful create would silently report back the wrong code. It's not that `GET` is missing something `POST` has; it's the reverse — `POST` needs an override because its correct code differs from the library default, while `GET`'s correct code happens to *be* the default. The same reasoning will apply to `DELETE` once it exists: the correct code there is usually `204 No Content`, which also isn't the default and will need its own explicit `status_code=status.HTTP_204_NO_CONTENT`.

**What it doesn't do:** decide *what a property is allowed to look like*, compute anything, or touch a repository directly. If `create_property` the route function started building `Property(...)` itself or calling `uow.properties.add(...)` directly, business logic would leak into the HTTP layer — the same "wrong layer" problem `repository-pattern.md` describes for `service.py` talking to `select()` directly instead of a repository.

## 3. `service.py`'s job — this is where business logic actually lives

```python
async def create_property(uow: UnitOfWork, data: PropertyCreate, host_id: uuid.UUID) -> Property:
    property_ = Property(host_id=host_id, **data.model_dump())
    uow.properties.add(property_)
    await uow.session.flush()
    return property_
```

This is deliberately the *one* layer allowed to contain business rules — contrast with `repository.py`, which `core/repository.py`'s own docstring says explicitly must not ("Data access only, no business rules"). `create_property` doesn't have an interesting rule to enforce yet (it's close to a pure passthrough), but this is where one would go — the AC's own example from `BOOK-28`, `guest_count <= property.max_guests`, will live in `reservation/service.py`'s eventual create-reservation function, not in `ReservationRepository`.

**Why the `flush()` matters here, concretely:** `Property.created_at` is a server-side default — it doesn't exist in Python until the `INSERT` actually runs. `commit()` doesn't happen until the whole request finishes (`get_uow()`'s `__aexit__`, after this function has already returned). Without the `flush()`, `PropertyRead`'s response would have to build with an empty `created_at`, which isn't allowed by the schema. `flush()` runs the `INSERT` now, populates `property_.created_at` in Python, without ending the transaction.

## 3.1 `update_property` — mutating an already-tracked object instead of building a new one

```python
async def update_property(uow: UnitOfWork, id: uuid.UUID, data: PropertyUpdate) -> Property | None:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(property_, field, value)
    return property_
```

This is a different shape from `create_property` in two ways worth being precise about.

**`setattr` doesn't touch the database — it changes Python memory now, and SQL follows later, separately.** `setattr(property_, "title", "New Title")` is plain Python (equivalent to `property_.title = "New Title"`, just with the attribute name as a variable) — nothing to do with SQL by itself. But `title` here isn't a plain attribute: it's a SQLAlchemy-instrumented one (from `Mapped[str]` in `models.py`), which intercepts the assignment via Python's descriptor protocol and marks the object "dirty" in the session's bookkeeping. That's still just in-memory tracking — **no `UPDATE` is sent to Postgres at the moment `setattr` runs.** The route returns `property_` with the new value already visible in Python; the router builds `PropertyRead` straight from that in-memory object, and the response goes out to the client. Only *after* that, in `get_uow()`'s `__aexit__`, does `session.commit()` actually run and send `UPDATE properties SET title = ... WHERE id = ...` — meaning the database is genuinely updated *after* the client already has the response, same ordering `create_property`'s `flush()` note above works around, just without needing to work around it here (nothing in the response depends on data only the database can produce).

**No `uow.properties.add(property_)` here, unlike `create_property` — and that's correct, not an omission.** `add()` exists for exactly one situation: a brand-new object the session has never seen (`Property(...)`, just constructed, no row in the database yet) — the session doesn't know it exists until you hand it over explicitly. `update_property` never constructs a new object; `get_active_by_id` *loads* one via `select(...)`, and loading an object through a session automatically registers it in that session's **identity map** ([`sqlalchemy-basics.md`](../db/sqlalchemy-basics.md)) — being loaded already *is* being tracked. `setattr`-ing an attribute on an already-tracked object is enough on its own; there's no separate "start watching this" step to opt into.

| Situation | Session already tracking it? | Needs `add()`? |
|---|---|---|
| `Property(...)` — just constructed (`create_property`) | No | Yes |
| Loaded via `get_active_by_id`/`select(...)` (`update_property`) | Yes, automatically | No |

## 3.2 `delete_property` — soft delete, and why `status_code`/`-> None` have to agree

```python
async def delete_property(uow: UnitOfWork, id: uuid.UUID) -> bool:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return False
    property_.deleted_at = datetime.now(UTC)
    return True
```

```python
@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property(property_id: uuid.UUID, uow: UnitOfWork = Depends(get_uow)) -> None:
    deleted = await service.delete_property(uow, property_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
```

**Deliberately not `uow.properties.delete(property_)`.** That's the inherited `BaseRepository` hard delete, which `core/README.md`'s "repository.py" section already says won't stop misuse — soft delete belongs in `service.py` specifically *because* it's a business rule (a delist decision), not data access. `deleted_at` has no `server_default`, unlike `created_at`: a server-side default only makes sense for a value true for *every* row, computable by the database alone with no business decision involved. `created_at` qualifies (every row has a creation moment); `deleted_at` doesn't (most rows never get one, and *whether* one applies is a decision only `service.py` can make) — so it has to be set explicitly here, in Python, when a delist actually happens.

**`status_code=status.HTTP_204_NO_CONTENT` and `-> None` aren't two unrelated choices — they have to agree.** `status_code` (the decorator argument) controls the response's *status line* (`HTTP/1.1 204 No Content`), independent of whatever the function returns. `-> None`, with no `response_model` declared, controls the *body* — nothing here for FastAPI to serialize as JSON, so the body is empty. These have to match: HTTP requires a `204` response to have **no body at all**. Returning the deleted `Property` here instead — the way `create_property`/`update_property` return theirs — would be wrong for this specific status code, not just superfluous. Same reasoning as `POST`'s explicit `status_code=status.HTTP_201_CREATED` from section 2: FastAPI's default status code is always `200`, which is wrong for both `POST` (should be `201`) and `DELETE` (should be `204`) — both need an explicit override, for different REST-correct reasons.

## 3.3 `create_review` (`BOOK-73`) — custom exceptions, when `None` can't say enough

Every `property` endpoint so far only had one way to fail besides validation: "not found." Returning `None` and letting the router turn that into a `404` was enough. `POST /reservations/{id}/reviews` has **four** distinct failure reasons, each needing a *different* HTTP status — one `None` can't distinguish "reservation doesn't exist" from "reservation exists but isn't completed" from "you're not a participant" from "you already reviewed this." Trying to cram that into a single sentinel return value (`None`, `False`, or similar) would force the router to guess which failure happened.

```python
# app/review/service.py
class ReservationNotFoundError(Exception): ...
class ReservationNotCompletedError(Exception): ...
class NotAParticipantError(Exception): ...
class DuplicateReviewError(Exception): ...

async def create_review(uow, reservation_id, author_id, data) -> Review:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    if reservation.status != ReservationStatus.COMPLETED:
        raise ReservationNotCompletedError
    ...
    if author_id not in participant_ids:
        raise NotAParticipantError
    ...
    try:
        await uow.session.flush()
    except IntegrityError as exc:
        raise DuplicateReviewError from exc
    return review
```

```python
# app/review/router.py
try:
    return await service.create_review(uow, reservation_id, author_id, data)
except service.ReservationNotFoundError as exc:
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found") from exc
except service.ReservationNotCompletedError as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Reservation is not completed") from exc
except service.NotAParticipantError as exc:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="...") from exc
except service.DuplicateReviewError as exc:
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="...") from exc
```

This keeps the same discipline `get_property`'s `None` already established — `service.py` still never imports `HTTPException` or anything FastAPI-specific, it just has a richer vocabulary than `None`/not-`None` for saying *what* went wrong. The router is still the only place that decides what HTTP status a given failure maps to.

**The duplicate-review check is the DB's `UNIQUE (reservation_id, author_id)` constraint, not a Python `if`.** `create_review` doesn't query for an existing review first — it just tries the `flush()` and catches `IntegrityError` if the constraint rejects it. This avoids a race: a check-then-insert (`if no existing review: insert`) has a gap between the check and the insert where two concurrent requests could both pass the check before either commits — the same "an app-level check can't stop concurrent double-booking" reasoning `docs/db/concurrency-basics.md` already makes for `Reservation`'s `EXCLUDE` constraint. The `UNIQUE` constraint is the actual source of truth; catching its violation is just how that gets turned into a clean `409` instead of a raw database error reaching the client.

## 4. Why split them into two files at all

**Different concerns.** `router.py` deals with "how does this arrive over HTTP and how does it leave over HTTP" — query strings, status codes, response shaping. `service.py` deals with "what is actually allowed to happen" — the same "what the business rule needs" vs. "how it's expressed" split `repository-pattern.md` section 1 makes for repositories, one layer further out.

**Testability, again.** A test can call `service.create_property(fake_uow, data, host_id)` directly — no HTTP, no FastAPI `TestClient`, no running server — and check the business logic in isolation, the same testability argument `repository-pattern.md` section 4 makes for repositories vs. inline queries.

## 5. Where `schemas.py` fits — the translation happens at the router boundary

`service.py` never imports `PropertyRead`. It takes `PropertyCreate` in (already-validated input) and returns a real `Property` ORM instance out — the same object shape `repository.py` already works with. The *router* is what converts between the API's contract and the domain's objects in both directions:

* incoming: FastAPI parses JSON into `PropertyCreate` before `create_property` (the route) even runs
* outgoing: FastAPI takes the `Property` instance the route returns and builds `PropertyRead` from it via `response_model` + `ConfigDict(from_attributes=True)` ([`pydantic-schemas-basics.md`](pydantic-schemas-basics.md))

`service.py` sits entirely on the "domain object" side of that boundary — it never sees a schema class, only ever `Property`.

## 6. Full request trace, end to end, for `POST /properties`

1. Client sends `POST /properties?host_id=<uuid>` with a JSON body.
2. FastAPI parses the body against `PropertyCreate` — a bad field fails here with `422`, before any of our code runs.
3. `Depends(get_uow)` runs: opens a `UnitOfWork`, which opens a session and builds every module's repository on it.
4. The route calls `service.create_property(uow, data, host_id)`.
5. `service.py` constructs a `Property`, calls `uow.properties.add(...)` (inherited from `BaseRepository`), then `flush()`s to populate `created_at`.
6. The route returns the `Property` instance; FastAPI builds `PropertyRead` from it via `response_model`.
7. The response is sent to the client — **then** `get_uow()`'s `__aexit__` runs, committing the transaction.

Note the order in step 7: the client already has the response before the commit finishes. This is normal — it's the same "response first, cleanup after" shape `get_db()` already has, just now with a commit attached to the cleanup instead of only a session close.

## 7. `Query(...)` — `Field`'s counterpart for query-string parameters

`GET /properties` takes several filters as query-string parameters (`?limit=5&city=Tel+Aviv&max_guests=2`), not as a JSON body:

```python
@router.get("", response_model=list[PropertyRead])
async def list_properties(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    city: str | None = None,
    guests: int | None = Query(default=None, gt=0),
    min_price_cents: int | None = Query(default=None, ge=0),
    max_price_cents: int | None = Query(default=None, ge=0),
    uow: UnitOfWork = Depends(get_uow),
) -> list[Property]:
```

`Query(...)` is `Field(...)`'s counterpart for this case — same job (attach constraints: a default, `ge`/`le`/`gt`), same underlying Pydantic validation machinery, just declared on a bare function parameter instead of an attribute inside a `BaseModel`. `city: str | None = None` doesn't need `Query(...)` at all — it's still read from the query string (same inference rule that already applies to `host_id` in `create_property`: a plain scalar, not part of the URL path, is read from the query string by default) — `Query(...)` is only needed when there's an actual constraint to declare beyond a bare default, which `city` doesn't have here.

**Why these aren't bundled into a schema class the way `PropertyCreate` bundles the request body:** the real difference is what's actually arriving on the wire. A request body is genuinely *one JSON object* — the client sends one cohesive blob, so one Pydantic class naturally represents it. Query parameters are *independent `key=value` pairs* in the URL — not one object, just several loosely-related knobs. Wrapping six independent, optional filters in their own class would be extra structure that doesn't reflect how they actually arrive, for no benefit at this scale. (FastAPI does support grouping query params into a class too, worth reaching for if the same filter-shape were reused across several endpoints — not the case here.)

**`guests` vs. `Property.max_guests`:** worth flagging since the two are easy to conflate. `guests` is the size of the group *searching* — a minimum requirement, not a cap — compared against each property's own `max_guests` capacity as `Property.max_guests >= guests` in `repository.py`. It used to be named `max_guests` too, matching the column it filters on, which read as though it meant "cap the property's max_guests at this value" — the opposite of what it actually does. Renamed to `guests` precisely to stop implying an upper bound it never was.

### 7.1 Why filters are query params here, not a JSON body — even though `GET` *could* technically have one

Four compounding reasons, not just convention for its own sake:

1. **`GET` requests aren't supposed to carry a body at all.** Per the HTTP spec, a body on `GET` "has no defined semantics" — servers, proxies, and caches are free to ignore or strip it. In practice a lot of infrastructure (browsers, CDNs, some HTTP client libraries) doesn't reliably send or forward one. Relying on a `GET` body is fragile in a way relying on query params isn't — query params are the only standard way to parameterize a `GET`.

2. **Caching.** `GET` is meant to be safe and cacheable — that's the whole REST contract for it (no side effects, same input → same output, cacheable by intermediaries). Every caching layer (browser cache, CDN, reverse proxy) keys its cache off the URL, query string included. A filter expressed as `?city=Tel+Aviv&guests=2` is naturally part of that cache key; a filter buried in a body isn't visible to a cache at all, so two different searches could collide in a cache, or nothing would get cached correctly.

3. **Shareable/bookmarkable.** Because query params live in the URL, `GET /properties?city=Tel+Aviv&guests=2` is a complete, copyable, linkable thing — paste it in a browser, bookmark it, put it in a `<a href>`, and you get the same filtered results. A JSON body isn't part of the URL, so a filtered search expressed that way couldn't be shared as a link at all — you'd need to also transmit the body out-of-band.

4. **It matches what the request is actually for.** A body represents "the resource I'm sending you" — that's why `POST /properties` has one (`PropertyCreate` is the new property). `GET /properties` isn't sending a resource, it's asking a question with some filters attached — query params are the idiomatic way to parameterize a read, not describe a payload.

**Mechanically, in this code:** FastAPI decides query-vs-body per parameter based on two things — the HTTP method, and the parameter's type. Every parameter in `list_properties` is a plain scalar (`int`, `str | None`, etc.), not a Pydantic `BaseModel` — that's what makes FastAPI treat them as query params automatically (`Query(...)` just adds validation on top, the same way `Field(...)` does for a body field). Contrast with `create_property`'s `data: PropertyCreate` — that's a `BaseModel` type, which is what tells FastAPI "parse this from the JSON body" instead. So the query-vs-body split isn't a special rule for `GET` specifically — it falls out of "which type is this parameter," and it happens to line up with `GET` because nobody in this codebase typed a `BaseModel` parameter on a `GET` route.

Mechanically, FastAPI decides query-vs-body per *parameter*, based on its type, not the HTTP method directly: a plain scalar type (`int`, `str | None`, `uuid.UUID`) becomes a query/path param; a `BaseModel` type (like `PropertyCreate`) becomes the parsed body. Every parameter on `list_properties` is a scalar, which is what actually makes them query params — it falls out of the type each one was given, and happens to line up with `GET` because nothing here typed a `BaseModel` parameter on a `GET` route.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `router.py` | HTTP-shaped concerns only — parses via schemas, calls `service.py`, shapes the response. No business logic, no direct repository calls. |
| `service.py` | The one layer allowed to hold business rules. Takes validated schema input in, returns ORM instances out — never imports a `*Read`/`*Create` schema itself. |
| `response_model` | Tells FastAPI which schema to build from whatever the route returns, via `ConfigDict(from_attributes=True)` |
| Translation boundary | The router, not `service.py`, is where schema ↔ ORM-object conversion happens, in both directions |
| Request order | Route returns → response sent to client → `get_uow()`'s `__aexit__` commits, after the fact |
| `Query(...)` | `Field`'s counterpart for a bare function parameter read from the URL's query string, not the body |
| `setattr(obj, field, value)` | Plain Python, not SQL — changes an in-memory attribute now; the `UPDATE` it eventually causes only fires at flush/commit, later |
| `add()` vs. loaded-via-`select()` | `add()` is only for objects the session has never seen; anything loaded through the session (`get_active_by_id`, etc.) is already tracked automatically via the identity map |
| `status_code` vs. `-> None` | `status_code` sets the response's status line; `-> None` (no `response_model`) sets an empty body. A `204` response requires both — they have to agree, not just happen to match |
| Why `deleted_at` has no `server_default` | Only appropriate for a value true for every row and computable by the DB alone; whether a property is delisted is a business decision, not a database fact — so it's set explicitly in `service.py`, never by Postgres |
| Custom `service.py` exceptions | Used when `None`/not-`None` can't distinguish enough outcomes (`BOOK-73`'s 4 failure reasons); `service.py` still never imports `HTTPException` — the router catches each and decides the status code |
| Duplicate check via `IntegrityError` | Relying on the DB's `UNIQUE` constraint (catch-after-flush) instead of a check-then-insert avoids a race between two concurrent requests — same reasoning as `Reservation`'s `EXCLUDE` constraint |
