import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class NotificationBase(DeclarativeBase):
    """BOOK-64: Notification's own model catalog, not the monolith's Base.

    Every table registered on app.core.db.models.Base belongs to Booking's
    database and Booking's migrations. Notification's tables live in its own
    database (step 4c), with its own migrations -- so they register here,
    and Booking's Alembic stops seeing them at all.
    """


class ProcessedNotificationEvent(NotificationBase):
    """Inbox-pattern dedup marker (worker-decisions.md §13). Existence of the
    row is all that matters -- processed_at is only for debugging.
    """

    __tablename__ = "processed_notification_events"

    # BOOK-64: a plain UUID, no foreign key into Booking's outbox_events --
    # the two tables are about to live in different databases, and a foreign
    # key can't cross databases (microservices-basics.md §3.2)
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
