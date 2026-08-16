# Pydantic schemas basics (BOOK-28)

Started as theory before touching anything, same approach as [`repository-pattern.md`](repository-pattern.md) was originally — now updated in place: `app/*/schemas.py` are real, implemented files (sections 3.1–3.5 below reflect what's actually there, not a plan).

## 1. Why Pydantic specifically

FastAPI is built *around* Pydantic, not just validation-adjacent to it. One Pydantic class definition does three separate jobs for FastAPI:

1. **Parses incoming JSON into typed Python objects.** A request body arrives as raw JSON text; Pydantic converts it into a real Python object with real types, rejecting anything that doesn't fit (wrong type, missing required field, out-of-range value) with a structured `422` response — before your route's code even runs.
2. **Generates the interactive API docs** (Swagger UI / ReDoc). FastAPI reads a Pydantic class's fields and types directly to produce documentation that's always in sync with the code — no separate doc-writing step that can drift.
3. **Serializes outgoing Python objects back to JSON.** Same class, opposite direction: a route returns a Python object, Pydantic converts it to the JSON response.

Skip Pydantic and you'd be hand-writing all three yourself: manual validation, hand-maintained docs, manual dict-to-JSON conversion.

## 2. Why the API's shape must not be the DB model's shape

Four concrete failure modes if a route accepts or returns a SQLAlchemy model directly instead of a separate schema:

**Leaking sensitive fields.** `User.hashed_password` is a real column. Serialize the SQLAlchemy `User` object directly as a response and that hashed password lands in the JSON — a real leak, not hypothetical. A `UserRead` schema simply has no `hashed_password` field at all, so it's structurally impossible to leak, not just "we remembered to exclude it this time."

**Letting clients set fields they shouldn't control.** If the request body's shape is the DB model, a client can send `{"id": "<their-own-uuid>", "created_at": "2020-01-01", ...}` with nothing to stop it. A `PropertyCreate` schema has no `id`/`created_at` fields — there's no field to smuggle a value into. The server always generates those itself.

**Coupling the public API to internal storage details.** The DB model's shape is driven by *storage* concerns — normalization, foreign keys, what other tables' relationships need. The API's shape should be driven by what *clients* need — sometimes identical, often not (e.g. `PropertyRead` might want a computed `average_rating` that isn't a column at all, or nest `host: {...}` instead of a raw `host_id`). If the API *is* the DB model, any DB schema change — renaming a column, splitting a table — breaks every client immediately, even when the change has nothing to do with what clients actually use.

**Validation with a good error message, at the right point.** Checking `Review.rating` is between 1 and 5 in the schema rejects a bad request instantly with a clear `422`, before the database is touched at all. Relying only on the DB's `CheckConstraint` means the request travels all the way to a failed `INSERT` first, producing a much uglier, harder-to-parse database error, after already doing wasted work opening a transaction.

**Same underlying motivation as the repository pattern's split** ([`repository-pattern.md`](repository-pattern.md) section 1: "what the business rule needs" vs. "how SQLAlchemy expresses it"). Here it's "what the client sends/receives" vs. "how the DB stores it" — two different concerns, two different classes, even where they look nearly identical today, because that separation is what stops them from becoming *accidentally* coupled later.

## 3. What this will look like, per module

Per the AC, each module's `schemas.py` gets multiple Pydantic classes for the same model, one per direction:

```python
# app/property/schemas.py (simplified excerpt -- see the real file for
# the actual Field(...) constraints and docstring comments, section 3.5)
from pydantic import BaseModel, ConfigDict

class PropertyCreate(BaseModel):
    title: str
    description: str
    city: str
    country: str
    max_guests: int
    price_per_night_cents: int
    # no id, no created_at, no deleted_at -- server-generated, client can't set them

class PropertyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    host_id: uuid.UUID
    title: str
    description: str
    city: str
    country: str
    max_guests: int
    price_per_night_cents: int
    created_at: datetime
```

`model_config = ConfigDict(from_attributes=True)` is what lets `PropertyRead.model_validate(prop)` build the schema directly from a SQLAlchemy `Property` object's attributes, instead of manually copying each field across by hand — where `prop` is whatever `PropertyRepository(session).get_by_id(id)` ([`repository-pattern.md`](repository-pattern.md) section 3) handed back: a real `Property` ORM instance, not a dict.

Without the flag, `model_validate` only accepts dict-like input — it reads fields via `obj["title"]`, not `obj.title` — so handing it `prop` directly raises an error. The two options without the flag:

```python
prop = await PropertyRepository(session).get_by_id(id)  # a Property ORM instance

# Option 1: construct field-by-field by hand
schema = PropertyRead(
    id=prop.id,
    host_id=prop.host_id,
    title=prop.title,
    description=prop.description,
    city=prop.city,
    country=prop.country,
    max_guests=prop.max_guests,
    price_per_night_cents=prop.price_per_night_cents,
    created_at=prop.created_at,
)

# Option 2: convert to a dict first, then validate that
schema = PropertyRead.model_validate(vars(prop))
```

Both work, but both rot the moment a field is added to the model and forgotten here — `from_attributes=True` reads the class's declared fields off the object automatically, so there's nothing to keep in sync by hand.

`*Update` schemas (for `PATCH`) will need every field optional, since a partial update only sends the fields actually changing — a different shape again from both `Create` and `Read`, not a variant reusing either.

## 3.1 Which schema is used where — and why `*Read` isn't only for `GET`

Only two of the three classes ever appear as a *request* body — `*Create` and `*Update`. `*Read` never parses a request; it's built the other direction, from an ORM instance (section above).

But `*Read` isn't the response only to `GET`. REST convention is that creating or updating a resource returns *the resource itself*, in the same shape `GET` would return it — so `PropertyRead` is reused as the response body for `POST` (create) and `PATCH` (update) too, not just `GET`:

| Endpoint | Request body | Response body |
|---|---|---|
| `POST /properties` (create) | `PropertyCreate` | `PropertyRead` |
| `GET /properties/{id}` | — | `PropertyRead` |
| `PATCH /properties/{id}` (update) | `PropertyUpdate` | `PropertyRead` |
| `DELETE /properties/{id}` | — | usually no body (`204`) |

One `*Read` class, reused across three endpoints — not a separate `*CreateResponse`/`*UpdateResponse` per operation. That's only worth splitting out if a response's shape genuinely differs from `GET`'s (e.g. a signup response that includes a one-time token `GET` never repeats) — none of this project's modules need that yet.

**Naming: why `Create`/`Read`/`Update`, not `Request`/`Response`.** Considered and deliberately kept as-is. `Create` and `Update` already unambiguously mean "this is a request body" — a `Request` suffix on top (`PropertyCreateRequest`) would repeat information the verb already gives, for no added clarity. `Read` is the only one of the three that's genuinely ambiguous by name — per the table above, it's really "the shape sent back to the client," not "only used for reads" — but splitting *that* into `Response` still leaves `Create`/`Update` as the more useful names for what they are, so the class stays `*Read` (this is also FastAPI's own tutorial convention: `UserCreate`/`UserOut`/`UserUpdate`). Read `*Read` as "the resource's public shape," not literally "only for `GET`."

## 3.2 Schemas are only the client-facing slice — the server fills in the rest from elsewhere

A `*Create` schema's fields are exactly, and only, what the client is trusted to send in the request body. It is **not** the full set of data that ends up on the row — some of that comes from *other* sources the schema deliberately says nothing about, most commonly the authenticated request itself.

Concretely, on `Property`: `PropertyCreate` has no `host_id` field. But every `Property` row needs one — it can't be left blank. Where it comes from: the client's JWT, not the JSON body. A FastAPI dependency (`get_current_user`, [app/README.md](../../app/README.md)) decodes the `Authorization: Bearer <token>` header on the request and resolves it to the actual `User` who's logged in. The router then combines the two — schema fields from the body, `host_id` from the token — when building the `Property`:

```python
@router.post("/properties", response_model=PropertyRead)
async def create_property(
    payload: PropertyCreate,                        # only what the client sent
    current_user: User = Depends(get_current_user),  # resolved from the JWT, not the body
):
    prop = Property(**payload.model_dump(), host_id=current_user.id)
    ...
```

`payload.model_dump()` only ever contains `PropertyCreate`'s fields — `host_id` is merged in afterward, from a value the server derived itself, not one it trusted the client to state.

**This is deliberate, not incidental — the same reasoning as excluding `id`/`created_at` (section 2), applied to identity instead of generated values.** If `host_id` were a field on `PropertyCreate`, nothing would stop a client from sending someone *else's* user id and creating a listing that isn't actually theirs. Any field that answers "who does this belong to" must come from who the server has *authenticated*, never from a value the client is trusted to declare honestly.

The same pattern recurs on every module with an ownership/authorship field:

| Model | Field missing from `*Create` | Filled in from |
|---|---|---|
| `Property` | `host_id` | `current_user.id` (the logged-in host) |
| `Reservation` | `guest_id` | `current_user.id` (the logged-in guest) |
| `Review` | `author_id` | `current_user.id` (guest or host, whichever is reviewing) |

Not yet wired up anywhere in this codebase — `auth/router.py` and `get_current_user` don't exist yet (`app/README.md` describes them as planned). This section documents the intended shape so it isn't re-derived when BOOK-29 actually builds the routes.

## 3.3 Other server-computed fields excluded from `*Create` — not just ownership

Section 3.2 covered *ownership* fields (`host_id`, `guest_id`, `author_id`) coming from auth instead of the request body. The same exclusion applies to a second category: fields the server computes or manages as part of a state machine, even though nothing about them is "who does this belong to."

`ReservationCreate` ([app/reservation/schemas.py](../../app/reservation/schemas.py)) is the clearest example. The client sends only `property_id`, `check_in`, `check_out`, `guest_count` — three more fields are missing that `ReservationRead` still exposes:

- **`total_cents`** — computed as `property.price_per_night_cents * nights` in `service.py`, not sent by the client. Letting the client name its own price would let it just pay less than the host is actually charging.
- **`status`** — always starts at `ReservationStatus.PENDING` on creation; only server-side logic (payment success, cancellation, the expiry job) ever moves it from there.
- **`expires_at`** — set to `datetime.now() + <hold window>` at creation time, a constant the server decides, not derived from the request at all. Read it as "how long this `PENDING` hold is good for": creating a reservation immediately blocks its dates via the `EXCLUDE` constraint ([docs/db/exclude-constraint-basics.md](../db/exclude-constraint-basics.md)), even before payment — `expires_at` is the cutoff that stops an abandoned checkout from blocking those dates forever. It clears back to `null` once the reservation reaches `CONFIRMED`, and it's what Epic 6's (not yet built) expiry job filters on to cancel abandoned holds.

None of these three ever appear in `ReservationCreate`; all three appear in `ReservationRead`. The rule from section 3.2 generalizes: `*Create` schemas carry only what the client actually decides — server-computed state (whether it's identity, money, or a lifecycle timestamp) never goes in `*Create`, no matter how "creation-adjacent" it feels.

## 3.4 Reservation and Payment are two separate creates, in sequence — not one action

`PaymentCreate.reservation_id` ([app/payment/schemas.py](../../app/payment/schemas.py)) is easy to misread as "the reservation being created alongside this payment." It isn't — it references a reservation that already exists.

The actual flow: `POST /reservations` creates a `Reservation` immediately, in `PENDING` status, with `expires_at` set (section 3.3) — a hold, not yet a booking, but one that already blocks those dates for other guests via the `EXCLUDE` constraint, before any money has moved. Then, as a *separate* action, `POST /reservations/{id}/pay` (or `POST /payments`) sends a `PaymentCreate` referencing that same, already-created reservation's id. On success, the reservation transitions `PENDING` → `CONFIRMED` and `expires_at` clears.

This is also why `PaymentCreate` is nearly empty — just `reservation_id`, with `kind`/`amount_cents`/`status` excluded by the same section 3.3 rule. Worth flagging as an open question, not a settled decision: since "pay for this reservation" naturally lives at a URL like `POST /reservations/{id}/pay`, `reservation_id` might belong in the *path* rather than a request body — in which case `PaymentCreate` may not need to exist as a body schema at all. That's a route-shape call for `BOOK-29`, not decided here.

## 3.5 Every constraint in the domain, and which layer actually checks it

Every DB `CheckConstraint`/`UniqueConstraint`/`ExcludeConstraint` in this codebase falls into one of three tiers, and which tier it's in *dictates* whether a schema-level check is even possible — this isn't a style choice per constraint, it's a hard structural limit on what Pydantic can see.

**Tier 1 — single field, self-contained.** Nothing needed but the value itself. Always schema-checkable with a plain `Field(...)` constraint.

**Tier 2 — multiple fields, same request body.** Needs more than one field, but all of them are already in the payload being validated. Schema-checkable, but needs a `@model_validator(mode="after")` instead of a per-field `Field(...)`, since it compares fields against each other rather than checking one in isolation.

**Tier 3 — needs another row.** Needs data that isn't in this request at all — a different table's row, or other existing rows of the *same* table. Structurally impossible to check in a Pydantic schema, which validates one JSON body in isolation with no DB session. These are DB-constraint-only, and/or a `service.py` check after the relevant row is actually loaded.

| Constraint | DB enforcement | Tier | Schema-level check |
|---|---|---|---|
| `Property.max_guests > 0` | `CheckConstraint` | 1 | `PropertyCreate/Update.max_guests = Field(gt=0)` |
| `Property.price_per_night_cents >= 0` | `CheckConstraint` | 1 | `Field(ge=0)` |
| `Reservation.guest_count > 0` | `CheckConstraint` | 1 | `Field(gt=0)` |
| `Review.rating BETWEEN 1 AND 5` | `CheckConstraint` | 1 | `Field(ge=1, le=5)` |
| `User` password strength | *(none — DB only stores the hash)* | 1 | `UserCreate.password = Field(min_length=8)` |
| `User` email format | *(none — DB only enforces uniqueness)* | 1 | `UserCreate/Read/Update.email: EmailStr` |
| `Reservation.check_out > check_in` | `CheckConstraint` | 2 | `ReservationCreate`'s `@model_validator` |
| `Reservation.guest_count <= Property.max_guests` | *(none — app-level only)* | 3 | ❌ not possible — needs the referenced `Property` row; enforced in `service.py` after loading it (`docs/db/er-diagram.md`'s "Key constraints") |
| No overlapping reservations per property | `ExcludeConstraint` (`no_overlapping_reservations`) | 3 | ❌ not possible — needs every *other* reservation on that property, and only the DB can make this race-proof under concurrency (`docs/db/exclude-constraint-basics.md`) |
| At most one succeeded charge per reservation | Partial unique index | 3 | ❌ not possible — needs every *other* payment on that reservation |
| `User.email` unique | Unique index | 3 | ❌ not possible — needs every *other* user row |
| `Review` unique per `(reservation_id, author_id)` | `UniqueConstraint` | 3 | ❌ not possible — needs every *other* review row |

**Why keep the DB constraint even where the schema already checks the same thing (tiers 1–2)?** Defense in depth, not redundancy: the schema check only runs for requests that go through this particular FastAPI route. The DB constraint is the backstop that holds regardless of how a row gets written — `scripts/verify_no_overlapping_reservations.py`, `core/seed.py`, a future admin script, or a bug in `service.py` that bypasses the schema entirely. The schema check exists purely for a *better error, earlier* (section 2) — it's never the only thing standing between bad data and the table.

## 4. Where this stops

This ticket defines the schemas themselves — nothing about wiring them into actual routes. `BOOK-29` is what builds real endpoints that use `PropertyCreate`/`PropertyRead` as request/response types.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| Pydantic | Validates incoming JSON into typed objects, powers auto-generated API docs, serializes outgoing objects to JSON — all from one class |
| `*Create` schema | What a client sends to create a resource — no server-generated fields (`id`, `created_at`) |
| `*Read`/`*Out` schema | What the API returns — reused as the response for `GET`, `POST` (create), and `PATCH` (update) alike, not just `GET`; never includes sensitive/internal fields (`hashed_password`) |
| `*Update` schema | For partial updates (`PATCH`) — every field optional, distinct shape from `Create`/`Read` |
| `ConfigDict(from_attributes=True)` | Lets a response schema be built directly from a SQLAlchemy model instance, not manual field mapping |
| Why not reuse the DB model | Prevents leaking sensitive columns, prevents clients setting server-generated fields, decouples API contract from storage schema, enables schema-level validation with clearer errors |
