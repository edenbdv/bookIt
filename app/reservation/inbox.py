import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.models import Base


class ProcessedPaymentEvent(Base):
    """BOOK-65: Booking's Inbox for payment-events (worker-decisions.md §13).
    Existence of the row is all that matters -- processed_at is only for
    debugging.

    Saved in the same transaction as the confirm/cancel it guards, so a
    redelivered event can never act twice, even across a crash -- unlike
    Notification's Inbox, whose side effect (an email) can't share a
    transaction with its row.
    """

    __tablename__ = "processed_payment_events"

    # plain UUID, no FK -- the event comes from Payment's outbox, which ends
    # up in another database (microservices-basics.md §3.2)
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
