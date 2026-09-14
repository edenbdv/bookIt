import uuid
from datetime import datetime

from sqlalchemy import select

from app.core.db.repository import BaseRepository
from app.payment.models import Payment, PaymentKind, PaymentStatus


class PaymentRepository(BaseRepository[Payment]):
    model = Payment

    # BOOK-76: at most one row can ever match -- the partial unique index
    # on (reservation_id) WHERE kind='charge' AND status='succeeded'
    # (models.py) guarantees it, so scalar_one_or_none() is safe here.
    async def get_succeeded_charge_by_reservation_id(
        self, reservation_id: uuid.UUID
    ) -> Payment | None:
        result = await self.session.execute(
            select(Payment).where(
                Payment.reservation_id == reservation_id,
                Payment.kind == PaymentKind.CHARGE,
                Payment.status == PaymentStatus.SUCCEEDED,
            )
        )
        return result.scalar_one_or_none()

    # no unique constraint backs this one -- a TIMEOUT can genuinely leave
    # more than one PENDING charge for the same reservation (each retry
    # mints its own row), so this takes any one as "there's already an
    # attempt in flight", not the only possible match
    async def get_pending_charge_by_reservation_id(
        self, reservation_id: uuid.UUID
    ) -> Payment | None:
        result = await self.session.execute(
            select(Payment).where(
                Payment.reservation_id == reservation_id,
                Payment.kind == PaymentKind.CHARGE,
                Payment.status == PaymentStatus.PENDING,
            )
        )
        return result.scalars().first()

    # BOOK-101: both kinds -- a REFUND can be left PENDING too
    async def list_stale_pending(self, older_than: datetime) -> list[Payment]:
        result = await self.session.execute(
            select(Payment).where(
                Payment.status == PaymentStatus.PENDING,
                Payment.created_at < older_than,
            )
        )
        return list(result.scalars())
