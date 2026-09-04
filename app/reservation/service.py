import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db.unit_of_work import UnitOfWork
from app.reservation.models import (
    IdempotencyKey,
    IdempotencyKeyStatus,
    Reservation,
    ReservationStatus,
)
from app.reservation.schemas import ReservationCreate

# docs/db/idempotency-basics.md's ~24h window
IDEMPOTENCY_KEY_TTL_HOURS = 24

# how long a PENDING reservation holds its dates before Epic 6's expiry job
# would clear an abandoned one -- no expiry job exists yet, so nothing
# actually enforces this window today; it's set now so that job has a real
# value to filter on once it exists (er-diagram.md's Reservation.expires_at)
PENDING_HOLD_MINUTES = 15


class PropertyNotFoundError(Exception):
    pass


class SelfBookingError(Exception):
    pass


class GuestCountExceededError(Exception):
    pass


class ReservationConflictError(Exception):
    pass


class IdempotencyKeyInProgressError(Exception):
    pass


class IllegalTransitionError(Exception):
    pass


class ReservationNotFoundError(Exception):
    pass


class NotReservationGuestError(Exception):
    pass


# CANCELLED and COMPLETED map to an empty set: terminal states, no legal
# transition out of either. BOOK-38 (cancel) and any future confirm/complete
# logic should go through transition_to() below rather than setting
# .status directly, so this table stays the one place the rules live.
_LEGAL_TRANSITIONS: dict[ReservationStatus, set[ReservationStatus]] = {
    ReservationStatus.PENDING: {ReservationStatus.CONFIRMED, ReservationStatus.CANCELLED},
    ReservationStatus.CONFIRMED: {ReservationStatus.CANCELLED, ReservationStatus.COMPLETED},
    ReservationStatus.CANCELLED: set(),
    ReservationStatus.COMPLETED: set(),
}


def transition_to(reservation: Reservation, new_status: ReservationStatus) -> None:
    if new_status not in _LEGAL_TRANSITIONS[reservation.status]:
        raise IllegalTransitionError(
            f"cannot transition reservation from {reservation.status} to {new_status}"
        )
    reservation.status = new_status


# stand-in for Epic 8's (BOOK-10) real trigger: a payment webhook calling
# transition_to() on success. Manually callable for now so the state machine
# is testable end-to-end before payments exist -- see docs/domain-layer/state-machine-basics.md
async def confirm_reservation(uow: UnitOfWork, reservation_id: uuid.UUID) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    transition_to(reservation, ReservationStatus.CONFIRMED)
    return reservation


# guest-only for now (BOOK-38) -- host-initiated cancellation is a
# different, penalized action on real platforms (refund/notification/strike
# behavior all differ), split off to BOOK-80 rather than folded in here
async def cancel_reservation(
    uow: UnitOfWork, reservation_id: uuid.UUID, current_user_id: uuid.UUID
) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    # checked after existence, not before -- 404 for a nonexistent
    # reservation takes priority over 403 for a real one you don't own,
    # same order as property/service.py's update_property
    if reservation.guest_id != current_user_id:
        raise NotReservationGuestError
    transition_to(reservation, ReservationStatus.CANCELLED)
    return reservation


async def create_reservation(
    uow: UnitOfWork, data: ReservationCreate, guest_id: uuid.UUID
) -> Reservation:
    property_ = await uow.properties.get_active_by_id(data.property_id)
    if property_ is None:
        raise PropertyNotFoundError

    # host booking their own property: out of scope, see er-diagram.md
    if property_.host_id == guest_id:
        raise SelfBookingError

    # cross-table rule, can't live in ReservationCreate's own validation
    if data.guest_count > property_.max_guests:
        raise GuestCountExceededError

    # snapshot total_cents now -- a later price change shouldn't retroactively
    # alter what this guest already agreed to pay
    nights = (data.check_out - data.check_in).days
    total_cents = nights * property_.price_per_night_cents

    # EXCLUDE uses a GiST index, not a UNIQUE B-tree -- overlapping rows are
    # separate physical entries, so two concurrent inserts can both land
    # uncommitted at once and then deadlock discovering each other (a
    # UNIQUE/B-tree insert can't, only one row can ever hold that exact key).
    # This lock removes that race by serializing inserts per property_id.
    # xact-scoped: released automatically on commit/rollback.
    # (docs/db/concurrency-basics.md sec.4.1)
    await uow.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:property_id))"),
        {"property_id": str(data.property_id)},
    )

    reservation = Reservation(
        property_id=data.property_id,
        guest_id=guest_id,
        check_in=data.check_in,
        check_out=data.check_out,
        guest_count=data.guest_count,
        total_cents=total_cents,
        expires_at=datetime.now(UTC) + timedelta(minutes=PENDING_HOLD_MINUTES),
    )
    uow.reservations.add(reservation)
    try:
        # flush, not commit: runs the EXCLUDE constraint's check now. Caught
        # broadly since every other integrity cause is already ruled out
        # above -- see docs/db/integrity-error-basics.md
        await uow.session.flush()
    except IntegrityError as exc:
        raise ReservationConflictError from exc
    return reservation


async def create_reservation_idempotent(
    uow: UnitOfWork, data: ReservationCreate, guest_id: uuid.UUID, idempotency_key: str
) -> Reservation:
    # insert-first, not check-then-insert: the key column's PK constraint is
    # what actually closes the concurrent-duplicate-key race (two requests
    # with the same key can't both "see nothing yet" and both proceed) --
    # see docs/db/idempotency-basics.md's concurrent-key race section
    key_row = IdempotencyKey(
        key=idempotency_key,
        expires_at=datetime.now(UTC) + timedelta(hours=IDEMPOTENCY_KEY_TTL_HOURS),
    )
    uow.session.add(key_row)
    try:
        await uow.session.flush()
    except IntegrityError:
        # the failed flush left the transaction aborted -- roll back before
        # this session can run any more queries
        await uow.session.rollback()
        existing = await uow.session.get(IdempotencyKey, idempotency_key)
        if existing.status == IdempotencyKeyStatus.COMPLETED:
            existing_reservation = await uow.reservations.get_by_id(existing.reservation_id)
            # keys aren't scoped per-guest in this table -- without this check,
            # a different guest reusing/guessing someone else's key would get
            # back that other guest's reservation as their own POST's result
            if existing_reservation.guest_id != guest_id:
                raise IdempotencyKeyInProgressError
            return existing_reservation
        # still PROCESSING: a genuinely concurrent request for the same key
        # is mid-flight right now -- reject rather than poll/wait, same
        # simplicity call as BOOK-35's broad IntegrityError catch
        raise IdempotencyKeyInProgressError

    # if create_reservation raises, the UoW rolls back this whole request's
    # transaction on the way out. flush() above isn't commit() -- the
    # PROCESSING row was only sent to Postgres, never made permanent -- so
    # rollback erases it too, not just the failed attempt. no row survives,
    # so the same key is free to retry from scratch (docs/db/idempotency-basics.md)
    reservation = await create_reservation(uow, data, guest_id)
    key_row.status = IdempotencyKeyStatus.COMPLETED
    key_row.reservation_id = reservation.id
    return reservation
