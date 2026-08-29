import uuid
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.reservation.models import ReservationStatus

# The server compares check_in against its own UTC date, but a guest picks
# dates in their local one -- at 00:30 in UTC+13 the guest's "today" is
# still yesterday in UTC. A day of slack keeps a legitimate same-day
# booking from being rejected over that gap; it's wide enough to cover
# every real offset (UTC-12..+14) and still refuses an actually-past stay.
BOOKING_PAST_SLACK_DAYS = 1


class ReservationCreate(BaseModel):
    property_id: uuid.UUID
    check_in: date
    check_out: date
    # matches the DB's CheckConstraint("guest_count > 0"), rejected here
    # guest_count <= property.max_guests is NOT checked here:
    # that's a cross-table rule needing the referenced property's row,
    # which schema validation has no DB access to look up -- it belongs
    # in service.py instead (docs/db/er-diagram.md's "Key constraints")
    guest_count: int = Field(gt=0)
    # no id, no guest_id, no total_cents, no status, no expires_at, no
    # created_at: guest_id comes from the authenticated current user,
    # total_cents is computed server-side from the property's nightly
    # price (a client-supplied price could undercut what the host is
    # charging), status/expires_at/created_at are all server-managed
    # state for the booking lifecycle

    @model_validator(mode="after")
    def check_out_after_check_in(self) -> "ReservationCreate":
        # matches the DB's CheckConstraint("check_out > check_in"). Unlike
        # guest_count <= max_guests, this one CAN be checked here: both
        # fields are already in this same request body, no other table's
        # row needs to be looked up first
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self

    @model_validator(mode="after")
    def check_in_not_in_past(self) -> "ReservationCreate":
        # Has no DB CheckConstraint behind it, unlike check_out > check_in:
        # "past" moves every day, so a stored constraint would retroactively
        # invalidate rows that were perfectly legal when written. It lives
        # here for the same reason the rule above does -- the only input it
        # needs is already in this request body (plus the clock).
        #
        # check_out needs no check of its own: it's already > check_in.
        #
        # Only guards creation. Existing past-dated rows stay valid, which
        # is what lets the seed's COMPLETED stay (app/core/seed.py) and any
        # real completed booking keep their history.
        earliest = datetime.now(UTC).date() - timedelta(days=BOOKING_PAST_SLACK_DAYS)
        if self.check_in < earliest:
            raise ValueError("check_in cannot be in the past")
        return self


class ReservationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    property_id: uuid.UUID
    guest_id: uuid.UUID
    check_in: date
    check_out: date
    guest_count: int
    total_cents: int
    status: ReservationStatus
    expires_at: datetime | None
    created_at: datetime


class ReservationUpdate(BaseModel):
    # only guest_count is a plain field edit. Changing check_in/check_out
    # would need to re-run the overlap check the EXCLUDE constraint
    # enforces (docs/db/exclude-constraint-basics.md) and re-price
    # total_cents -- closer to cancel-and-rebook than a field patch, so
    # it's left out here. status transitions (confirm/cancel) belong on
    # their own action endpoints (e.g. POST .../cancel), not a generic
    # PATCH, since each transition has its own side effects (payment
    # refund, freeing the EXCLUDE constraint's dates)
    guest_count: int | None = Field(default=None, gt=0)
