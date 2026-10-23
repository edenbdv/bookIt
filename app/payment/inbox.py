import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.models import Base


class ProcessedBookingEvent(Base):
    """BOOK-65: Payment's Inbox for booking-events (worker-decisions.md §13)
    -- a duplicate refund is real money. Existence of the row is all that
    matters; processed_at is only for debugging. Saved in the same commit
    as the PENDING refund row it guards, before the provider is called.
    """

    __tablename__ = "processed_booking_events"

    # plain UUID, no FK -- the event comes from Booking's outbox, which ends
    # up in another database (microservices-basics.md §3.2)
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
