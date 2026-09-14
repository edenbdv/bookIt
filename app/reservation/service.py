import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db.unit_of_work import UnitOfWork
from app.core.idempotency.models import (
    IDEMPOTENCY_KEY_TTL_HOURS,
    IdempotencyKey,
    IdempotencyKeyStatus,
)
from app.core.outbox.models import OutboxEvent, OutboxEventType
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.schemas import ReservationCreate

# how long a PENDING reservation holds its dates before BOOK-46's scheduled
# job (app/worker.py's expire_pending_reservations) cancels an abandoned
# one -- see docs/jobs/worker-decisions.md section 5.
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


# same key reused for a request that doesn't match the original (different
# property/dates/guest_count) -- Stripe's actual behavior for this case:
# reject explicitly, don't silently hand back the old result and don't
# silently treat it as a new request either
class IdempotencyKeyMismatchError(Exception):
    pass


class IllegalTransitionError(Exception):
    pass


class ReservationNotFoundError(Exception):
    pass


class NotReservationGuestError(Exception):
    pass


class NotPropertyHostError(Exception):
    pass


# BOOK-85: same "participant" idea review/service.py's NotAParticipantError
# already uses -- a reservation is visible to its own guest OR the
# property's host, no one else
class NotAParticipantError(Exception):
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


# BOOK-49. Thin payload -- consumers re-fetch current state by id (same
# pattern as BOOK-45's email job), so a duplicate/delayed at-least-once
# delivery never hands out stale data. Call after transition_to() succeeds.
#
# event_id is generated here, not left to the id column's ORM-level default,
# so the same value can go in both the row and the payload -- BOOK-51's
# consumer dedups on it (worker-decisions.md §13), and that only works if
# the id a consumer reads back out of Kafka is the same one the row itself
# ends up with, not whatever the ORM happens to assign at flush time.
def _emit_outbox_event(
    uow: UnitOfWork, reservation: Reservation, event_type: OutboxEventType
) -> None:
    event_id = uuid.uuid4()
    uow.session.add(
        OutboxEvent(
            id=event_id,
            event_type=event_type,
            reservation_id=reservation.id,
            payload={
                "event_id": str(event_id),
                "event_type": event_type.value,
                "reservation_id": str(reservation.id),
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
    )


# system-triggered cancel -- no guest-ownership check. For callers that
# aren't an HTTP request from the reservation's own guest: the expiry job
# below, and BOOK-54's saga compensation. cancel_reservation() is the
# guest-authorized HTTP path; this is what it and system callers share.
def cancel_reservation_system(uow: UnitOfWork, reservation: Reservation) -> None:
    transition_to(reservation, ReservationStatus.CANCELLED)
    _emit_outbox_event(uow, reservation, OutboxEventType.BOOKING_CANCELLED)


# stand-in for Epic 8's (BOOK-10) real trigger: a payment webhook calling
# transition_to() on success. Manually callable for now so the state machine
# is testable end-to-end before payments exist -- see docs/domain-layer/state-machine-basics.md
async def confirm_reservation(uow: UnitOfWork, reservation_id: uuid.UUID) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    transition_to(reservation, ReservationStatus.CONFIRMED)
    _emit_outbox_event(uow, reservation, OutboxEventType.BOOKING_CONFIRMED)
    return reservation


# guest-only -- host-initiated cancellation is host_cancel_reservation below
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
    cancel_reservation_system(uow, reservation)
    return reservation


# BOOK-80: host-cancel counterpart to cancel_reservation -- authorized by
# property host, not reservation guest
async def host_cancel_reservation(
    uow: UnitOfWork, reservation_id: uuid.UUID, current_user_id: uuid.UUID
) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    property_ = await uow.properties.get_by_id(reservation.property_id)
    if property_.host_id != current_user_id:
        raise NotPropertyHostError
    cancel_reservation_system(uow, reservation)
    return reservation


# BOOK-102: a host's own bookings view, for the "my properties" page
async def list_reservations_for_property(
    uow: UnitOfWork, property_id: uuid.UUID, current_user_id: uuid.UUID
) -> list[Reservation]:
    property_ = await uow.properties.get_active_by_id(property_id)
    if property_ is None:
        raise PropertyNotFoundError
    if property_.host_id != current_user_id:
        raise NotPropertyHostError
    return await uow.reservations.list_by_property_id(property_id)


# BOOK-85: a guest's own bookings view, for the "my bookings" page
async def list_reservations_for_guest(
    uow: UnitOfWork, guest_id: uuid.UUID, limit: int, offset: int
) -> list[Reservation]:
    return await uow.reservations.list_by_guest_id(guest_id, limit, offset)


# BOOK-85: single fetch, visible to the reservation's own guest OR the
# property's host -- same data either way, nothing branches on which
async def get_reservation(
    uow: UnitOfWork, reservation_id: uuid.UUID, current_user_id: uuid.UUID
) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    property_ = await uow.properties.get_by_id(reservation.property_id)
    if current_user_id not in (reservation.guest_id, property_.host_id):
        raise NotAParticipantError
    return reservation


# BOOK-46. Called from app/worker.py's scheduled task, which owns
# constructing the real UnitOfWork -- kept here, uow-first like every other
# service function, so it's testable with a FakeUnitOfWork (no DB), same as
# cancel_reservation/confirm_reservation above.
async def expire_pending_reservations(uow: UnitOfWork) -> int:
    reservations = await uow.reservations.list_expired_pending(datetime.now(UTC))
    for reservation in reservations:
        cancel_reservation_system(uow, reservation)
    return len(reservations)


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
            # same key, different request body than the original attempt
            if (
                existing_reservation.property_id != data.property_id
                or existing_reservation.check_in != data.check_in
                or existing_reservation.check_out != data.check_out
                or existing_reservation.guest_count != data.guest_count
            ):
                raise IdempotencyKeyMismatchError
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

    # BOOK-45. Lives here, not the router: only this branch is a genuinely
    # new reservation -- a retried request returns the existing one above,
    # so it can't double-send. on_commit() (not a direct .delay()) fires
    # only after the transaction commits (worker-decisions.md §9). Local
    # import to dodge a circular import -- worker.py imports this module too.
    from app.worker import send_reservation_received_email

    uow.on_commit(lambda: send_reservation_received_email.delay(str(reservation.id)))

    return reservation
