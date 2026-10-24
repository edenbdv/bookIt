"""BOOK-65: Payment's demo data, in Payment's own database. Run after Booking's seed:

    uv run python -m app.payment.migrate
    uv run python -m app.payment.seed

Idempotent, same as Booking's seed -- fixed ids, so a re-run finds the rows instead of
duplicating them. A charge per paid demo reservation, plus a charge+refund pair for
the cancelled one (a realistic lifecycle, not just a missing charge).
"""

import asyncio
import uuid

from app.payment.db import PaymentUnitOfWork
from app.payment.models import Payment, PaymentKind, PaymentStatus

# the demo reservations in Booking's seed (app/core/db/seed.py) -- copied, not
# imported: Payment knows other services' rows by id only
RESERVATION_CAROL_TLV = uuid.UUID("00000000-0000-0000-0000-000000000201")
RESERVATION_DAVE_HAIFA = uuid.UUID("00000000-0000-0000-0000-000000000202")
RESERVATION_CAROL_HAIFA_CANCELLED = uuid.UUID("00000000-0000-0000-0000-000000000203")

# (payment id, reservation id, kind, amount_cents) -- all SUCCEEDED
_PAYMENTS = [
    (
        uuid.UUID("00000000-0000-0000-0000-000000000301"),
        RESERVATION_CAROL_TLV,
        PaymentKind.CHARGE,
        180000,
    ),
    (
        uuid.UUID("00000000-0000-0000-0000-000000000302"),
        RESERVATION_DAVE_HAIFA,
        PaymentKind.CHARGE,
        96000,
    ),
    (
        uuid.UUID("00000000-0000-0000-0000-000000000303"),
        RESERVATION_CAROL_HAIFA_CANCELLED,
        PaymentKind.CHARGE,
        64000,
    ),
    (
        uuid.UUID("00000000-0000-0000-0000-000000000304"),
        RESERVATION_CAROL_HAIFA_CANCELLED,
        PaymentKind.REFUND,
        64000,
    ),
]


async def seed(uow: PaymentUnitOfWork) -> None:
    for payment_id, reservation_id, kind, amount_cents in _PAYMENTS:
        if await uow.payments.get_by_id(payment_id) is None:
            uow.payments.add(
                Payment(
                    id=payment_id,
                    reservation_id=reservation_id,
                    kind=kind,
                    amount_cents=amount_cents,
                    status=PaymentStatus.SUCCEEDED,
                )
            )


async def main() -> None:
    async with PaymentUnitOfWork() as uow:
        await seed(uow)
    print("Payment seed data ready.")


if __name__ == "__main__":
    asyncio.run(main())
