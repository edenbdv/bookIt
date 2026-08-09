import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Date, DateTime, Enum, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base

if TYPE_CHECKING:
    from app.auth.models import User
    from app.payment.models import Payment
    from app.property.models import Property
    from app.review.models import Review


class ReservationStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"  # a reservation can be cancelled by the guest or by the host (Epic 8's saga/idempotency work needs that history)
    COMPLETED = "completed"


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("check_out > check_in", name="check_out_after_check_in"),
        CheckConstraint("guest_count > 0", name="guest_count_positive"),
        # overlap prevention (EXCLUDE USING gist on property_id + daterange) is BOOK-24
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"))
    guest_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    guest_count: Mapped[int]
    # snapshot at booking time, not derived from property.price_per_night_cents —
    # a later price change must not alter what a guest already agreed to
    total_cents: Mapped[int]
    # values_callable: store the enum's string value, not its member name — see
    # the same note on User.role
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=ReservationStatus.PENDING,
    )
    # null once confirmed; used by Epic 6's expiry job to clear abandoned pending holds
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    property: Mapped["Property"] = relationship(back_populates="reservations")
    guest: Mapped["User"] = relationship(back_populates="reservations")
    # in-Python only, no column: mirrors Payment.reservation
    payments: Mapped[list["Payment"]] = relationship(back_populates="reservation")
    # in-Python only, no column: mirrors Review.reservation
    reviews: Mapped[list["Review"]] = relationship(back_populates="reservation")
