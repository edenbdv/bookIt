import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base

if TYPE_CHECKING:
    from app.auth.models import User
    from app.reservation.models import Reservation


class Property(Base):
    __tablename__ = "properties"
    __table_args__ = (
        CheckConstraint("max_guests > 0", name="max_guests_positive"),
        CheckConstraint("price_per_night_cents >= 0", name="price_non_negative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str]
    description: Mapped[str]
    # indexed: filtered on directly in availability search (BOOK-40)
    city: Mapped[str] = mapped_column(index=True)
    country: Mapped[str]
    max_guests: Mapped[int]
    price_per_night_cents: Mapped[int]
    # soft delete: null while listed, set on delist. Never hard-delete a
    # property with reservation history — see core/README.md's
    # "repository.py" section for why BaseRepository.delete() won't stop you.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # in-Python only, no column: derived from host_id, mirrors User.properties
    host: Mapped["User"] = relationship(back_populates="properties")
    # in-Python only, no column: mirrors Reservation.property
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="property")
