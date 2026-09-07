import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.models import Base


class OutboxEventType(str, enum.Enum):
    BOOKING_CONFIRMED = "booking_confirmed"
    BOOKING_CANCELLED = "booking_cancelled"


class OutboxEvent(Base):
    """Written in the same transaction as the domain change it describes
    (docs/jobs/outbox-basics.md §3) -- never on its own.

    No `relationship()` back to `Reservation` on purpose: this is core
    infrastructure, read generically by BOOK-50's relay, not by domain code
    navigating from a `Reservation` instance. A plain FK column keeps that
    one-way -- domain modules depend on core, not the other way around.
    `reservation_id` stays specific (not a generic aggregate_id) until a
    second real event source justifies generalizing, same call already made
    for `IdempotencyKey` (app/reservation/models.py).
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[OutboxEventType] = mapped_column(
        Enum(OutboxEventType, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
    )
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # NULL = not yet published; the relay fills this in once it publishes the row
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # the relay's query is always "WHERE published_at IS NULL" -- a
        # partial index keeps that cheap regardless of how large the
        # already-published history grows
        Index(
            "ix_outbox_events_unpublished",
            "published_at",
            postgresql_where=(published_at.is_(None)),
        ),
    )
