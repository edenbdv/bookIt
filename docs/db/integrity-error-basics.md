# `IntegrityError` basics (BOOK-35)

## 1. `IntegrityError` is generic — not specific to duplicates

`sqlalchemy.exc.IntegrityError` is SQLAlchemy's one wrapper class for *any* database integrity-rule violation, not just duplicates. Several genuinely different underlying problems all raise this exact same Python exception class:

| Underlying violation | Example in this codebase |
|---|---|
| `UNIQUE` violation | Two users with the same `email`; `review/service.py`'s duplicate-review check on `(reservation_id, author_id)` |
| `EXCLUDE` violation | `Reservation`'s overlapping-dates constraint — not a literal duplicate, a *conflicting* range |
| `FOREIGN KEY` violation | Inserting a row that references a `property_id`/`user_id` that doesn't actually exist |
| `CHECK` violation | `check_out > check_in`, `guest_count > 0`, if somehow violated at the DB level |
| `NOT NULL` violation | A required column left empty |

All five raise the same `IntegrityError` class at the SQLAlchemy level, regardless of which of these five actually happened.

## 2. The specific type is still there, one layer down

SQLAlchemy's `IntegrityError` isn't the *original* error — it's a wrapper around whatever the DB driver (`asyncpg`, here) actually raised, preserved on `exc.orig`. A real traceback from this project, hitting `Reservation`'s exclusion constraint:

```
sqlalchemy.exc.IntegrityError: (sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) <class 'asyncpg.exceptions.ExclusionViolationError'>: conflicting key value violates exclusion constraint "no_overlapping_reservations"
DETAIL:  Key (property_id, daterange(...))=(..., [2026-09-02,2026-09-05)) conflicts with existing key (...)=(..., [2026-09-01,2026-09-04)).
```

Notice `asyncpg.exceptions.ExclusionViolationError` right there in the message — that's the *specific* type, still fully available via `exc.orig`, SQLAlchemy just doesn't make you catch a different Python exception class for each one at the `except` level.

## 3. Why catching the generic `IntegrityError` is still safe here — reasoning, not the exception type

`review/service.py`'s existing `DuplicateReviewError` (and BOOK-35's `ReservationConflictError`, built the same way) both catch the bare `IntegrityError` at `flush()`, without checking `exc.orig`'s specific type. That's safe **only because every other possible cause has already been ruled out earlier in the same function**, by the time `flush()` runs:

- `property_id` was already confirmed to exist (fetched earlier) — an FK violation on it is already excluded
- `guest_id` is always valid — it's the authenticated user, never client-supplied
- `check_out > check_in`, `guest_count > 0` — already validated by the Pydantic schema before the service function even runs

So logically, by the point `flush()` executes, the *only* integrity rule left that could still fire is the one this function is actually trying to protect against (the unique review, or the exclusion constraint). Catching broadly is safe because of *that reasoning*, not because `IntegrityError` itself is precise — it very much isn't (section 1).

## 4. The trade-off, and the more defensive alternative

**Risk of catching broadly:** if an assumption above ever stops holding — a future code change removes the early existence check, or a genuine race condition introduces a different violation at this exact insert — the bare `except IntegrityError` would still fire, but for the *wrong* reason, and report a misleading error (e.g. "these dates are already booked" when the real problem was an unrelated FK violation).

**The more defensive alternative:** inspect `exc.orig` and check it's specifically the expected type before raising the domain exception:

```python
import asyncpg

try:
    await uow.session.flush()
except IntegrityError as exc:
    if isinstance(exc.orig.__cause__, asyncpg.exceptions.ExclusionViolationError):
        raise ReservationConflictError from exc
    raise  # some other integrity violation -- don't misreport it as a date conflict
```

**Why this codebase uses the simpler, broader catch instead:** consistency with the existing precedent (`review/service.py`'s `DuplicateReviewError`), and the earlier-validation reasoning in section 3 genuinely holds for both cases today. The defensive version is more correct in the abstract, but adds a dependency on `asyncpg`'s specific exception types leaking into `service.py` (which otherwise has no driver-specific imports at all), for a failure mode (an assumption silently becoming false later) that would show up immediately in testing rather than corrupting data silently.

**Note on industry practice:** if precision were wanted, the actually-portable way to get it isn't checking driver-specific Python exception classes (`asyncpg.exceptions.ExclusionViolationError`) — it's checking Postgres's own standardized **SQLSTATE code** (`exclusion_violation = 23P01`, `unique_violation = 23505`, etc.), exposed by every driver (`exc.orig.sqlstate` in asyncpg) because it's part of the SQL standard itself, not something any particular library invented. Both approaches (broad catch, or SQLSTATE-checked) are genuinely used in real systems; this codebase deliberately picked the broad one for BOOK-35, matching `review/service.py`'s existing precedent, since the reasoning in section 3 holds for this constraint too.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `IntegrityError` | SQLAlchemy's one generic wrapper for any DB integrity violation — `UNIQUE`, `EXCLUDE`, `FOREIGN KEY`, `CHECK`, `NOT NULL` all raise it |
| `exc.orig` | The original driver-level exception SQLAlchemy wrapped — where the *specific* type (e.g. `ExclusionViolationError`) actually lives |
| Broad `except IntegrityError` | Safe when every other possible cause is already ruled out earlier in the same function -- a reasoning-based guarantee, not a property of the exception type itself |
| Precise `exc.orig` check | More defensive, catches a wrong-assumption bug immediately instead of misreporting it, at the cost of driver-specific imports in `service.py` |
