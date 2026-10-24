import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.models import Base

# shared by every idempotent-create flow (docs/db/idempotency-basics.md's ~24h window)
IDEMPOTENCY_KEY_TTL_HOURS = 24


class IdempotencyKeyStatus(str, enum.Enum):
    PROCESSING = "processing"
    COMPLETED = "completed"


class IdempotencyKey(Base):
    """Core infrastructure, not a domain entity -- same reasoning as
    OutboxEvent. One nullable FK per resource (not a generic resource_type/
    resource_id pair) keeps a real DB constraint instead of an app-only check.
    """

    __tablename__ = "idempotency_keys"

    # client-supplied key is the primary key (docs/db/idempotency-basics.md)
    key: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[IdempotencyKeyStatus] = mapped_column(
        Enum(IdempotencyKeyStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=IdempotencyKeyStatus.PROCESSING,
    )
    # reference to the result, not a stored response body
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("reservations.id"))
    # BOOK-65: no ForeignKey in the model -- `payments` moved to Payment's own model
    # catalog (PaymentBase). The DB constraint still exists until the contract step
    # moves payment keys to Payment's own table and drops this column.
    payment_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # ~24h window (docs/db/idempotency-basics.md); swept by core/idempotency/cleanup.py
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
