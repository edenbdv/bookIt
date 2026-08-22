import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.unit_of_work import UnitOfWork
from app.reservation.models import IdempotencyKey, IdempotencyKeyStatus, Reservation
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


async def create_reservation(
    uow: UnitOfWork, data: ReservationCreate, guest_id: uuid.UUID
) -> Reservation:
    property_ = await uow.properties.get_active_by_id(data.property_id)
    if property_ is None:
        raise PropertyNotFoundError

    # a host can't book a stay at their own property (er-diagram.md's
    # "Deliberately not modelled yet" -- explicitly called out as this
    # ticket's job)
    if property_.host_id == guest_id:
        raise SelfBookingError

    # cross-table rule, deliberately left out of ReservationCreate's own
    # validation (see its comment in schemas.py) -- needs property's row
    if data.guest_count > property_.max_guests:
        raise GuestCountExceededError

    # price_per_night_cents is read here exactly once, to compute total_cents
    # now -- the stored result becomes the snapshot. Nothing ever re-reads
    # property_.price_per_night_cents again later to recompute total_cents
    # (e.g. on a future GET), so a later price change on the property can't
    # retroactively alter what this guest already agreed to pay.
    nights = (data.check_out - data.check_in).days
    total_cents = nights * property_.price_per_night_cents

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
        # flush, not commit: populates created_at, and is also what actually
        # runs the EXCLUDE constraint's check -- a genuine date conflict only
        # surfaces as an IntegrityError here, not before. Caught broadly
        # (not inspecting exc.orig's specific type) because every other
        # possible cause is already ruled out above: property_id exists
        # (fetched), guest_id is always valid (the authenticated user),
        # check_out > check_in and guest_count > 0 are already schema-
        # validated -- see docs/db/integrity-error-basics.md
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
