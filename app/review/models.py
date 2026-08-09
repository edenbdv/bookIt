import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base

if TYPE_CHECKING:
    from app.auth.models import User
    from app.reservation.models import Reservation


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_in_range"),
        # each party (guest or host) reviews a given stay once
        UniqueConstraint(
            "reservation_id", "author_id", name="uq_review_per_author_per_reservation"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"))
    # the guest reviewing the property, or the host reviewing the guest —
    # which one is inferred from whether author_id matches the reservation's
    # guest_id or the property's host_id, not a separate column
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int]
    comment: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    reservation: Mapped["Reservation"] = relationship(back_populates="reviews")
    author: Mapped["User"] = relationship(back_populates="reviews")
