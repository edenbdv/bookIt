from app.core.repository import BaseRepository
from app.payment.models import Payment


class PaymentRepository(BaseRepository[Payment]):
    model = Payment
