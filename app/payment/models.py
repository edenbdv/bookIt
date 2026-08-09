import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.models import Base

if TYPE_CHECKING:
    from app.reservation.models import Reservation


class PaymentKind(str, enum.Enum):
    CHARGE = "charge"
    REFUND = "refund"  # on a cancelled reservation, we refund the full amount of the charge (Epic 8's saga/idempotency work needs that history)


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_cents >= 0", name="amount_non_negative"),
        # at most one successful charge per reservation — failed/retried attempts
        # still get their own rows (Epic 8's saga/idempotency work needs that history)
        Index(
            "ix_payments_one_succeeded_charge_per_reservation",
            "reservation_id",
            unique=True,
            # partial index: only these rows count toward the uniqueness check
            postgresql_where=text("kind = 'charge' AND status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"))
    # values_callable: store the enum's string value, not its member name — see
    # the same note on User.role. The partial index below relies on this: its
    # raw SQL WHERE clause compares against 'charge'/'succeeded' directly.
    kind: Mapped[PaymentKind] = mapped_column(
        Enum(PaymentKind, values_callable=lambda enum_cls: [e.value for e in enum_cls])
    )
    amount_cents: Mapped[int]
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, values_callable=lambda enum_cls: [e.value for e in enum_cls]),
        default=PaymentStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    reservation: Mapped["Reservation"] = relationship(back_populates="payments")
