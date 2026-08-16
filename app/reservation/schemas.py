import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.reservation.models import ReservationStatus


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
