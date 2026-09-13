import uuid

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
