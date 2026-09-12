"""Manual verification for BOOK-54's split-commit fix.

Not a pytest test -- same reasoning as verify_idempotency_keys.py: this
needs to prove something about real Postgres transaction/commit behavior
that a FakeUnitOfWork can't meaningfully simulate (there's no real DB
fixture in this project yet, BOOK-61/62).

The guarantee under test: if react_to_payment_outcome() raises AFTER the
charge succeeded, the Payment row must still be durably SUCCEEDED -- not
rolled back along with the failed reaction step. Before the fix
(app/payment/saga.py's create_payment_and_react splitting the commit),
both would have shared one transaction and the charge's own record would
have been lost along with it.

Requires: `docker compose up -d db` already running.
Run with: uv run python scripts/verify_saga_commit_split.py
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.auth.models import User
from app.core.db.engine import get_engine
from app.core.db.unit_of_work import UnitOfWork
from app.payment.models import PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.saga import create_payment_and_react
from app.payment.schemas import PaymentCreate
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from app.review.models import Review  # noqa: F401

DEMO_EMAIL_SUFFIX = "_saga_commit_verify@example.com"
TODAY = datetime.now(UTC).date()


async def main() -> None:
    async with UnitOfWork() as uow:
        host = User(email=f"host{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        guest = User(email=f"guest{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        uow.session.add_all([host, guest])
        await uow.session.flush()

        prop = Property(
            host_id=host.id,
            title="Saga Commit-Split Test Villa",
            description="test",
            city="Tel Aviv",
            country="IL",
            max_guests=4,
            price_per_night_cents=10000,
        )
        uow.session.add(prop)
        await uow.session.flush()

        reservation = Reservation(
            property_id=prop.id,
            guest_id=guest.id,
            check_in=TODAY + timedelta(days=30),
            check_out=TODAY + timedelta(days=34),
            guest_count=2,
            total_cents=40000,
            status=ReservationStatus.PENDING,
        )
        uow.session.add(reservation)
        await uow.session.flush()
        guest_id, reservation_id = guest.id, reservation.id
    print(f"Created reservation {reservation_id}")

    # force SUCCESS so the charge definitely "happens", then make the
    # reaction step blow up right after -- simulating a crash/bug between
    # the two, which is exactly the window create_payment_and_react's split
    # commit exists to protect
    data = PaymentCreate(reservation_id=reservation_id)
    with patch(
        "app.payment.saga.reservation_service.confirm_reservation",
        side_effect=RuntimeError("simulated crash after charge succeeded"),
    ):
        try:
            async with UnitOfWork() as uow:
                await create_payment_and_react(
                    uow, data, guest_id, "commit-split-verify", force=PaymentOutcome.SUCCESS
                )
            print("UH OH: expected the simulated crash to propagate, it didn't")
        except RuntimeError:
            print("1. Reaction step crashed as expected (simulated)")

    # the crash above rolled back the SECOND transaction (the reservation
    # side) -- but the payment's own commit already happened before that,
    # in its own transaction, so it must still be there
    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text("SELECT status FROM payments WHERE reservation_id = :rid"),
            {"rid": str(reservation_id)},
        )
        row = result.first()

    if row is None:
        print("2. UH OH: no payment row survived -- the fix didn't work, the charge was lost")
    elif row[0] == PaymentStatus.SUCCEEDED.value:
        print(f"2. Payment row survived with status={row[0]!r} -- OK, charge record wasn't lost")
    else:
        print(f"2. UH OH: payment row survived but status={row[0]!r}, expected 'succeeded'")

    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text("SELECT status FROM reservations WHERE id = :rid"),
            {"rid": str(reservation_id)},
        )
        (resv_status,) = result.first()
    expected = ReservationStatus.PENDING.value
    ok = resv_status == expected
    print(
        f"3. Reservation status={resv_status!r} -- "
        f"{'OK, never confirmed since the reaction step crashed' if ok else 'UH OH, unexpected'}"
    )

    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM idempotency_keys WHERE key = 'commit-split-verify'"))
        await conn.execute(
            text("DELETE FROM payments WHERE reservation_id = :rid"), {"rid": reservation_id}
        )
        await conn.execute(
            text("DELETE FROM reservations WHERE id = :rid"), {"rid": reservation_id}
        )
        await conn.execute(text("DELETE FROM properties WHERE id = :pid"), {"pid": prop.id})
        await conn.execute(text(f"DELETE FROM users WHERE email LIKE '%{DEMO_EMAIL_SUFFIX}'"))
    print("cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
