"""Manual verification for BOOK-54's split-commit fix and its compensating refund.

Not a pytest test -- same reasoning as verify_idempotency_keys.py: this
needs to prove something about real Postgres transaction/commit behavior
that a FakeUnitOfWork can't meaningfully simulate (there's no real DB
fixture in this project yet, BOOK-61/62).

Two guarantees under test, both triggered by the same simulated crash
(confirm_reservation() raising right after a successful charge):

1. The Payment row must still be durably SUCCEEDED afterward -- not rolled
   back along with the failed reaction step. Before create_payment_and_react
   split the commit, charge + reaction shared one transaction, and the
   charge's own record would have been lost along with it.
2. The failure must be compensated, not just survived: a REFUND Payment row
   gets created, and the reservation ends up CANCELLED, not stuck PENDING
   forever with an unrefunded charge against it. The original exception
   still propagates afterward (see saga.py's react_to_payment_outcome) --
   compensating doesn't mean hiding that something went wrong.

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
    # commit (and the compensating refund) exist to protect
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
            print("1. Reaction step crashed as expected (simulated) -- and still propagated")

    # the crash rolled back the failed confirm attempt's own transaction --
    # but the charge's commit already happened before that, in its own
    # earlier transaction, so it must still be there
    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text("SELECT status FROM payments WHERE reservation_id = :rid AND kind = 'charge'"),
            {"rid": str(reservation_id)},
        )
        row = result.first()

    if row is None:
        print("2. UH OH: no charge payment survived -- the fix didn't work, the charge was lost")
    elif row[0] == PaymentStatus.SUCCEEDED.value:
        print(f"2. Charge payment survived with status={row[0]!r} -- OK, charge record wasn't lost")
    else:
        print(f"2. UH OH: charge payment survived but status={row[0]!r}, expected 'succeeded'")

    # the compensating action: a REFUND payment should now exist too
    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text(
                "SELECT status, kind FROM payments WHERE reservation_id = :rid AND kind = 'refund'"
            ),
            {"rid": str(reservation_id)},
        )
        refund_row = result.first()

    if refund_row is None:
        print(
            "3. UH OH: no refund payment was created -- the guest is charged with nothing to show for it"
        )
    else:
        print(
            f"3. Refund payment created with status={refund_row[0]!r} -- OK, charge was compensated"
        )

    # and the reservation should be CANCELLED, not stuck PENDING with an
    # unrefunded charge against it
    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text("SELECT status FROM reservations WHERE id = :rid"),
            {"rid": str(reservation_id)},
        )
        (resv_status,) = result.first()
    expected = ReservationStatus.CANCELLED.value
    ok = resv_status == expected
    print(
        f"4. Reservation status={resv_status!r} -- "
        f"{'OK, released since confirm failed and we compensated' if ok else 'UH OH, unexpected'}"
    )

    # FK order matters: idempotency_keys -> payments, processed_notification_events
    # -> outbox_events -> reservations. The notification-consumer may have
    # actually picked up the BookingCancelled event for real by the time
    # this runs (BOOK-51's dedup marker), hence that table too.
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM idempotency_keys WHERE key = 'commit-split-verify'"))
        await conn.execute(
            text(
                "DELETE FROM processed_notification_events WHERE event_id IN "
                "(SELECT id FROM outbox_events WHERE reservation_id = :rid)"
            ),
            {"rid": reservation_id},
        )
        await conn.execute(
            text("DELETE FROM outbox_events WHERE reservation_id = :rid"), {"rid": reservation_id}
        )
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
