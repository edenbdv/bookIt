import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db.models import Base

if TYPE_CHECKING:
    from app.property.models import Property
    from app.reservation.models import Reservation
    from app.review.models import Review


# permission tier only — hosting/booking are actions, not roles
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    # client-side default: id is available immediately, no DB round-trip needed
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    # the only publicly-showable name: email must never leave the API on a
    # public endpoint (see ReviewRead). Nullable here but REQUIRED in
    # UserCreate -- rows predating this column have no name and aren't
    # backfilled, so the column has to tolerate NULL even though every new
    # signup must supply one.
    display_name: Mapped[str | None]
    hashed_password: Mapped[str]
    # values_callable: store the enum's string value ("user"), not its member
    # name ("USER") — SQLAlchemy defaults to the name, which would silently
    # diverge from UserRole's own str values and from docs/db/er-diagram.md
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=UserRole.USER,
    )
    # server-side default: DB's clock is the source of truth, not the app's
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # in-Python only, no column: mirrors Property.host so both sides stay in sync
    properties: Mapped[list["Property"]] = relationship(back_populates="host")
    # in-Python only, no column: mirrors Reservation.guest (booking, not hosting)
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="guest")
    # in-Python only, no column: mirrors Review.author (guest reviewing a
    # property, or host reviewing a guest — same table, either direction)
    reviews: Mapped[list["Review"]] = relationship(back_populates="author")
