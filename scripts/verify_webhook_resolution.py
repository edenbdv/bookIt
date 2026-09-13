"""BOOK-55: live verification that a payment left PENDING by a crash during
charge()/refund() can be resolved later by the webhook -- the actual gap
BOOK-55 closes (payment-decisions.md §8).

Proves three things, not just "it runs":
1. Early commit -- the Payment/refund row survives a crash, doesn't vanish
2. Webhook -- a row left PENDING can be resolved afterward
3. Idempotency -- a repeat webhook delivery doesn't redo the reaction

A verification/dev tool, not runtime code the app depends on -- same
reasoning as verify_saga_commit_split.py: needs real Postgres commit/
rollback behavior a FakeUnitOfWork can't simulate.

Each scenario patches charge()/provider_refund() to raise right when
called -- simulating a crash *after* the row is already committed PENDING:

1. Charge crashes -> row survives PENDING -> webhook resolves SUCCESS ->
   reservation confirms. A repeat delivery is then a no-op.
2. Charge crashes -> webhook resolves FAILURE -> reservation cancels.
3. Refund crashes (triggered by a failed confirm) -> refund row survives
   PENDING -> webhook resolves it (no reaction needed -- REFUND, not CHARGE).

Requires: `docker compose up -d db` already running.
Run with: uv run python scripts/verify_webhook_resolution.py
"""

import asyncio
import sys
import uuid
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
from app.payment.saga import create_payment_and_react, resolve_payment_and_react
from app.payment.schemas import PaymentCreate
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from app.review.models import Review  # noqa: F401

DEMO_EMAIL_SUFFIX = "_webhook_verify@example.com"
TODAY = datetime.now(UTC).date()


async def _make_reservation(prop_id: uuid.UUID, guest_id: uuid.UUID, offset: int) -> uuid.UUID:
    async with UnitOfWork() as uow:
        reservation = Reservation(
            property_id=prop_id,
            guest_id=guest_id,
            check_in=TODAY + timedelta(days=30 + offset),
            check_out=TODAY + timedelta(days=34 + offset),
            guest_count=2,
            total_cents=40000,
            status=ReservationStatus.PENDING,
        )
        uow.session.add(reservation)
        await uow.session.flush()
        reservation_id = reservation.id
    return reservation_id


async def main() -> None:
    async with UnitOfWork() as uow:
        host = User(email=f"host{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        guest = User(email=f"guest{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        uow.session.add_all([host, guest])
        await uow.session.flush()

        prop = Property(
            host_id=host.id,
            title="Webhook Resolution Test Villa",
            description="test",
            city="Tel Aviv",
            country="IL",
            max_guests=4,
            price_per_night_cents=10000,
        )
        uow.session.add(prop)
        await uow.session.flush()
        guest_id, prop_id = guest.id, prop.id

    reservation_ids: list[uuid.UUID] = []

    # --- 1: charge crashes, webhook resolves SUCCESS, then a redelivery ---
    res1 = await _make_reservation(prop_id, guest_id, 0)
    reservation_ids.append(res1)
    with patch("app.payment.service.charge", side_effect=RuntimeError("simulated crash")):
        try:
            async with UnitOfWork() as uow:
                await create_payment_and_react(
                    uow, PaymentCreate(reservation_id=res1), guest_id, "webhook-verify-1"
                )
            print("UH OH: expected the simulated crash to propagate, it didn't")
        except RuntimeError:
            print("1. Charge crashed as expected -- checking the row survived")

    async with UnitOfWork() as uow:
        result = await uow.session.execute(
            text("SELECT id, status FROM payments WHERE reservation_id = :rid AND kind = 'charge'"),
            {"rid": str(res1)},
        )
        row = result.first()

    if row is None:
        print("2. UH OH: no payment row survived the crash -- the fix didn't work")
    elif row[1] == PaymentStatus.PENDING.value:
        print(f"2. Payment row survived as PENDING (id={row[0]}) -- OK, committed before the crash")
    else:
        print(f"2. UH OH: row survived but status={row[1]!r}, expected 'pending'")
    payment1_id = row[0]

    async with UnitOfWork() as uow:
        await resolve_payment_and_react(uow, payment1_id, PaymentOutcome.SUCCESS)

    async with UnitOfWork() as uow:
        (payment_status,) = (
            await uow.session.execute(
                text("SELECT status FROM payments WHERE id = :pid"), {"pid": str(payment1_id)}
            )
        ).first()
        (resv_status,) = (
            await uow.session.execute(
                text("SELECT status FROM reservations WHERE id = :rid"), {"rid": str(res1)}
            )
        ).first()

    ok = (
        payment_status == PaymentStatus.SUCCEEDED.value
        and resv_status == ReservationStatus.CONFIRMED.value
    )
    print(
        f"3. After webhook SUCCESS: payment={payment_status!r}, reservation={resv_status!r} -- "
        f"{'OK' if ok else 'UH OH, unexpected'}"
    )

    # redelivery -- deliberately a different outcome, to prove it's ignored, not reapplied
    async with UnitOfWork() as uow:
        await resolve_payment_and_react(uow, payment1_id, PaymentOutcome.FAILURE)

    async with UnitOfWork() as uow:
        (redelivered_status,) = (
            await uow.session.execute(
                text("SELECT status FROM payments WHERE id = :pid"), {"pid": str(payment1_id)}
            )
        ).first()

    print(
        f"4. Redelivered webhook (different outcome): status still {redelivered_status!r} -- "
        f"{'OK, no-op' if redelivered_status == PaymentStatus.SUCCEEDED.value else 'UH OH, redelivery changed it'}"
    )

    # --- 2: charge crashes, webhook resolves FAILURE -> reservation cancels ---
    res2 = await _make_reservation(prop_id, guest_id, 10)
    reservation_ids.append(res2)
    with patch("app.payment.service.charge", side_effect=RuntimeError("simulated crash")):
        try:
            async with UnitOfWork() as uow:
                await create_payment_and_react(
                    uow, PaymentCreate(reservation_id=res2), guest_id, "webhook-verify-2"
                )
        except RuntimeError:
            pass

    async with UnitOfWork() as uow:
        (payment2_id,) = (
            await uow.session.execute(
                text("SELECT id FROM payments WHERE reservation_id = :rid AND kind = 'charge'"),
                {"rid": str(res2)},
            )
        ).first()
        await resolve_payment_and_react(uow, payment2_id, PaymentOutcome.FAILURE)

    async with UnitOfWork() as uow:
        (resv2_status,) = (
            await uow.session.execute(
                text("SELECT status FROM reservations WHERE id = :rid"), {"rid": str(res2)}
            )
        ).first()

    print(
        f"5. After webhook FAILURE: reservation={resv2_status!r} -- "
        f"{'OK, cancelled' if resv2_status == ReservationStatus.CANCELLED.value else 'UH OH, unexpected'}"
    )

    # --- 3: refund crashes (triggered by a failed confirm), row survives ---
    res3 = await _make_reservation(prop_id, guest_id, 20)
    reservation_ids.append(res3)
    with (
        patch(
            "app.payment.saga.reservation_service.confirm_reservation",
            side_effect=RuntimeError("simulated confirm failure"),
        ),
        patch("app.payment.saga.provider_refund", side_effect=RuntimeError("simulated crash")),
    ):
        try:
            async with UnitOfWork() as uow:
                await create_payment_and_react(
                    uow,
                    PaymentCreate(reservation_id=res3),
                    guest_id,
                    "webhook-verify-3",
                    force=PaymentOutcome.SUCCESS,
                )
            print("UH OH: expected the simulated refund crash to propagate, it didn't")
        except RuntimeError:
            print("6. Refund crashed as expected -- checking the row survived")

    async with UnitOfWork() as uow:
        refund_row = (
            await uow.session.execute(
                text(
                    "SELECT id, status FROM payments WHERE reservation_id = :rid AND kind = 'refund'"
                ),
                {"rid": str(res3)},
            )
        ).first()

    if refund_row is None:
        print("7. UH OH: no refund row survived the crash -- the fix didn't work")
    elif refund_row[1] == PaymentStatus.PENDING.value:
        print(f"7. Refund row survived as PENDING (id={refund_row[0]}) -- OK")
    else:
        print(f"7. UH OH: refund row survived but status={refund_row[1]!r}")
    refund_payment_id = refund_row[0]

    async with UnitOfWork() as uow:
        await resolve_payment_and_react(uow, refund_payment_id, PaymentOutcome.SUCCESS)

    async with UnitOfWork() as uow:
        (refund_status,) = (
            await uow.session.execute(
                text("SELECT status FROM payments WHERE id = :pid"), {"pid": str(refund_payment_id)}
            )
        ).first()

    print(
        f"8. After webhook SUCCESS on refund: status={refund_status!r} -- "
        f"{'OK' if refund_status == PaymentStatus.SUCCEEDED.value else 'UH OH, unexpected'}"
    )

    # FK order: idempotency_keys -> payments, processed_notification_events -> outbox_events -> reservations
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM idempotency_keys WHERE key LIKE 'webhook-verify-%'"))
        for rid in reservation_ids:
            await conn.execute(
                text(
                    "DELETE FROM processed_notification_events WHERE event_id IN "
                    "(SELECT id FROM outbox_events WHERE reservation_id = :rid)"
                ),
                {"rid": rid},
            )
            await conn.execute(
                text("DELETE FROM outbox_events WHERE reservation_id = :rid"), {"rid": rid}
            )
            await conn.execute(
                text("DELETE FROM payments WHERE reservation_id = :rid"), {"rid": rid}
            )
            await conn.execute(text("DELETE FROM reservations WHERE id = :rid"), {"rid": rid})
        await conn.execute(text("DELETE FROM properties WHERE id = :pid"), {"pid": prop_id})
        await conn.execute(text(f"DELETE FROM users WHERE email LIKE '%{DEMO_EMAIL_SUFFIX}'"))
    print("cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
