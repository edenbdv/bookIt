import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, func
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.models import Base

if TYPE_CHECKING:
    from app.auth.models import User
    from app.property.models import Property
    from app.review.models import Review


class ReservationStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    guest_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    guest_count: Mapped[int]
    # snapshot at booking time -- a later price change shouldn't retroactively change this
    total_cents: Mapped[int]
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=ReservationStatus.PENDING,
    )
    # used by Epic 6's (not-yet-built) expiry job to clear abandoned pending holds
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("check_out > check_in", name="check_out_after_check_in"),
        CheckConstraint("guest_count > 0", name="guest_count_positive"),
        # DB-level double-booking prevention (docs/db/exclude-constraint-basics.md).
        # '[)' range: same-day turnover isn't an overlap. Cancelled rows excluded.
        ExcludeConstraint(
            (property_id, "="),
            (func.daterange(check_in, check_out, "[)"), "&&"),
            using="gist",
            where=(status != "cancelled"),
            name="no_overlapping_reservations",
        ),
    )

    property: Mapped["Property"] = relationship(back_populates="reservations")
    guest: Mapped["User"] = relationship(back_populates="reservations")
    reviews: Mapped[list["Review"]] = relationship(back_populates="reservation")
