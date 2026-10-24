import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from payment.db import PaymentBase


class PaymentIdempotencyKeyStatus(str, enum.Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"


# ~24h, same window as Booking's reservation keys (docs/db/idempotency-basics.md)
PAYMENT_IDEMPOTENCY_KEY_TTL_HOURS = 24


class PaymentIdempotencyKey(PaymentBase):
    """BOOK-65: POST /payments' idempotency keys -- split out of the shared
    idempotency_keys into Payment's own database. Not used until the switch;
    until then payment keys still go to the shared table."""

    __tablename__ = "payment_idempotency_keys"

    # client-supplied key is the primary key (docs/db/idempotency-basics.md)
    key: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[PaymentIdempotencyKeyStatus] = mapped_column(
        Enum(
            PaymentIdempotencyKeyStatus,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=PaymentIdempotencyKeyStatus.PROCESSING,
    )
    # a real FK again -- payments is in the same database
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
