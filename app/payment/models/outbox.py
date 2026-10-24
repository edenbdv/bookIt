import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.payment.db import PaymentBase


class PaymentEventType(str, enum.Enum):
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"


class PaymentOutboxEvent(PaymentBase):
    """BOOK-65: Payment's own outbox -- same pattern as core/outbox's
    OutboxEvent (docs/jobs/outbox-basics.md), but owned by Payment so it
    moves with Payment's own DB. Written in the same transaction as the
    payment's status change, never on its own. Published to `payment-events`.
    """

    __tablename__ = "payment_outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    event_type: Mapped[PaymentEventType] = mapped_column(
        Enum(PaymentEventType, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
    )
    # plain UUID, no FK -- also the Kafka partition key
    reservation_id: Mapped[uuid.UUID]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # NULL = not yet published; the relay fills this in once it publishes the row
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # same partial index as outbox_events -- the relay only ever asks
        # for unpublished rows
        Index(
            "ix_payment_outbox_events_unpublished",
            "published_at",
            postgresql_where=(published_at.is_(None)),
        ),
    )
