"""Payment's tables, one file per concept -- all on PaymentBase (services/payment/src/payment/db.py),
so they get Payment's own migrations and, from BOOK-65's switch, live in the
`payments` database. No foreign keys out of this package: other services' rows
are referred to by plain ids.

Re-exported here, so `from payment.models import X` works for every model --
and importing this package registers all tables on PaymentBase (Alembic needs that).
"""

from payment.models.idempotency import (
    PAYMENT_IDEMPOTENCY_KEY_TTL_HOURS,
    PaymentIdempotencyKey,
    PaymentIdempotencyKeyStatus,
)
from payment.models.inbox import ProcessedBookingEvent
from payment.models.outbox import PaymentEventType, PaymentOutboxEvent
from payment.models.payment import Payment, PaymentKind, PaymentStatus

__all__ = [
    "PAYMENT_IDEMPOTENCY_KEY_TTL_HOURS",
    "Payment",
    "PaymentEventType",
    "PaymentIdempotencyKey",
    "PaymentIdempotencyKeyStatus",
    "PaymentKind",
    "PaymentOutboxEvent",
    "PaymentStatus",
    "ProcessedBookingEvent",
]
