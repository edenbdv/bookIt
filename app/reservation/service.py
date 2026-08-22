import uuid
from datetime import UTC, datetime, timedelta

from app.core.unit_of_work import UnitOfWork
from app.reservation.models import Reservation
from app.reservation.schemas import ReservationCreate

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
    # flush, not commit: populates created_at, and is also what would raise
    # the EXCLUDE constraint's IntegrityError on a genuine date conflict --
    # catching that cleanly is BOOK-35, not this ticket
    await uow.session.flush()
    return reservation
