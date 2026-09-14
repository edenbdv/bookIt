# State machine basics (BOOK-37)

Written alongside the actual BOOK-37 work — `app/reservation/service.py`'s `_LEGAL_TRANSITIONS` table, `transition_to()`, and `confirm_reservation()` already exist by the time this doc exists, so the examples below are the real code.

## 1. The problem: an enum column alone enforces nothing

`Reservation.status` has been a `ReservationStatus` enum (`pending | confirmed | cancelled | completed`) since the model was first written, defaulting new rows to `pending`. But an enum column only constrains *which values* are legal — Postgres and SQLAlchemy both happily accept `reservation.status = ReservationStatus.CONFIRMED` regardless of what the status was a moment before. Nothing stops a stray line of code from setting a `cancelled` reservation straight to `completed`, or `completed` back to `pending` — every one of those is a value the column accepts, and none of them make business sense.

The fix isn't a new column or constraint — it's a single function every status change is required to go through, so the legality check lives in exactly one place instead of being re-implemented (or forgotten) at every call site.

## 2. The transition table

```python
_LEGAL_TRANSITIONS: dict[ReservationStatus, set[ReservationStatus]] = {
    ReservationStatus.PENDING: {ReservationStatus.CONFIRMED, ReservationStatus.CANCELLED},
    ReservationStatus.CONFIRMED: {ReservationStatus.CANCELLED, ReservationStatus.COMPLETED},
    ReservationStatus.CANCELLED: set(),
    ReservationStatus.COMPLETED: set(),
}
```

Each key is a *current* status; its value is the set of statuses it's legal to move to from there. `CANCELLED` and `COMPLETED` map to an **empty set**, not a missing key — they're terminal states, and the empty set says that explicitly: "the set of legal next states from here is nothing." That matters for `transition_to()` below, which looks the current status up unconditionally — a missing key would raise `KeyError` for a state nobody remembered to add, which is a bug; an empty set means "yes, this state was considered, and it really does have zero legal exits."

Dict-of-sets was chosen over a flat `set[tuple[from, to]]` for one reason: it makes terminal states visible as data (`set()`) rather than implicit (absence from a collection of pairs), which is easier to read as "here's every state and what it can do" — closer to how you'd actually draw the state diagram.

## 3. `transition_to()` — the one place this gets checked

```python
def transition_to(reservation: Reservation, new_status: ReservationStatus) -> None:
    if new_status not in _LEGAL_TRANSITIONS[reservation.status]:
        raise IllegalTransitionError(
            f"cannot transition reservation from {reservation.status} to {new_status}"
        )
    reservation.status = new_status
```

This doesn't touch the database directly — same shape as `router-service-basics.md`'s `update_property`: it mutates an already-tracked ORM instance in memory, and the actual `UPDATE` fires later, at commit. The function's only job is the legality check; *why* a caller wants a given transition (payment succeeded, guest cancelled, a background job saw the checkout date pass) is deliberately not its concern — that's each caller's job, decided outside this function.

Any future code that changes `.status` — `BOOK-38`'s cancel, Epic 6's completion job, Epic 8's payment webhook — is expected to call `transition_to()` rather than assign `.status` directly. That's a convention, not something Python enforces; nothing stops a future PR from writing `reservation.status = ReservationStatus.CANCELLED` directly and skipping the check entirely.

## 4. `confirm_reservation()` — a placeholder for a trigger that doesn't exist yet

```python
async def confirm_reservation(uow: UnitOfWork, reservation_id: uuid.UUID) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    transition_to(reservation, ReservationStatus.CONFIRMED)
    return reservation
```

`pending → confirmed` is conceptually "payment succeeded." When `BOOK-37` added this state machine, Epic 8 (`BOOK-10`, the payment saga and its webhook) didn't exist yet — rather than leave the transition rule itself untestable until it did, `BOOK-37` added `POST /reservations/{id}/confirm` as an explicit, manually-triggered stand-in: something a client (or a test) could call directly to move a reservation from `pending` to `confirmed` and see the state machine actually work end to end.

**This was a placeholder for who calls `transition_to()`, not a change to the rule itself — and that's exactly what let Epic 8 slot in without touching this file.** Epic 8 is built now (`docs/payment/payment-decisions.md`): on payment success, the saga calls `transition_to(reservation, ReservationStatus.CONFIRMED)` internally, the same function this endpoint always called. The confirmation doesn't *cause* payment; it's the reverse — payment succeeding is what causes confirmation, and the manual endpoint was only ever a stand-in for that real cause. It's still there today, unlocked, genuinely useful for testing the transition without a full payment round-trip — but `transition_to()` and its table never needed to change at all, which was the whole point of putting the check in one place.

Same reasoning applies to `confirmed → completed`: conceptually "the checkout date passed." Unlike `pending → confirmed` above, this one didn't need a manual stand-in — once the real trigger (a background job checking `check_out`) existed, it called `transition_to()` directly, same as every other caller (`docs/domain-layer/reservation-lifecycle.md` §7).

## 5. Why `IllegalTransitionError`, not a plain `ValueError`

Matches every other domain failure in `reservation/service.py` (`PropertyNotFoundError`, `SelfBookingError`, `ReservationConflictError`, etc., and `router-service-basics.md` §3.3's `ReservationNotFoundError`/`ReservationNotCompletedError`/etc. in `review/service.py`) — one exception class per distinct failure reason, caught individually in `router.py` and mapped to its own HTTP status. `service.py` still never imports `HTTPException` itself; the router is the only layer that decides `IllegalTransitionError` means `409 Conflict`, the same discipline `router-service-basics.md` §3.3 already describes.

---

## Cheat sheet

| Term | One-line meaning |
|---|---|
| `_LEGAL_TRANSITIONS` | `{current_status: {legal_next_statuses}}` — the one place the transition rules live |
| Empty set (`CANCELLED`/`COMPLETED`) | Explicit "no legal exit from here," not a missing/forgotten key |
| `transition_to()` | The single choke point every status change is expected to go through; checks legality, then mutates the in-memory object — no direct `UPDATE`, same as `update_property` |
| `confirm_reservation()` | A manual stand-in for Epic 8's payment webhook — same `transition_to()` call the real trigger will use later |
| Confirm vs. payment causality | Payment succeeding triggers confirmation, not the other way around — this endpoint only exists because that real trigger isn't built yet |
| `IllegalTransitionError` | Matches the one-exception-per-failure-reason pattern already established in `service.py`; router maps it to `409 Conflict` |
