"""BOOK-65: "Booking can't use this payment" end to end, against a real
Postgres and a real Kafka --

    payment_succeeded for a cancelled reservation -> Booking's handler
        -> booking_payment_rejected (Booking's outbox) -> Booking's relay
        -> booking-events -> Payment's consumer -> full refund
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from confluent_kafka import Consumer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox import relay
from app.payment import booking_consumer
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.payment_events import handle_payment_event
from tests.integration.helpers import make_pending_reservation

# first poll includes joining the group and the topic being auto-created
CONSUME_TIMEOUT_POLLS = 30


@pytest.fixture(autouse=True)
def _isolated_topic() -> Iterator[str]:
    # one fresh booking-events topic per test, for Booking's relay and Payment's consumer
    topic = f"booking-events-{uuid.uuid4().hex[:8]}"
    with patch.object(relay, "_TOPIC", topic), patch.object(booking_consumer, "TOPIC", topic):
        yield topic


@pytest.fixture
def kafka_consumer(_isolated_topic: str) -> Iterator[Consumer]:
    kafka = booking_consumer.make_consumer()  # real config: group "payment", manual commit
    kafka.subscribe([_isolated_topic])
    yield kafka
    kafka.close()


async def _refunds(payment_db_session: AsyncSession, reservation: Reservation) -> list[int]:
    result = await payment_db_session.scalars(
        select(Payment.amount_cents).where(
            Payment.reservation_id == reservation.id, Payment.kind == PaymentKind.REFUND
        )
    )
    return list(result)


@pytest.mark.anyio
async def test_a_payment_for_a_cancelled_reservation_is_refunded_through_kafka(
    app_db: None,
    kafka_bootstrap_servers: str,
    db_session: AsyncSession,
    payment_db_session: AsyncSession,
    kafka_consumer: Consumer,
) -> None:
    # money taken, but the reservation's hold already expired
    reservation, _ = await make_pending_reservation(db_session)
    charge = Payment(
        reservation_id=reservation.id,
        kind=PaymentKind.CHARGE,
        amount_cents=reservation.total_cents,
        status=PaymentStatus.SUCCEEDED,
    )
    # the charge in Payment's database, the reservation in Booking's (BOOK-65)
    payment_db_session.add(charge)
    await payment_db_session.commit()
    await db_session.execute(
        update(Reservation)
        .where(Reservation.id == reservation.id)
        .values(status=ReservationStatus.CANCELLED)
    )
    await db_session.commit()

    # Booking hears payment_succeeded -> writes booking_payment_rejected
    async with UnitOfWork() as uow:
        await handle_payment_event(
            uow,
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "payment_succeeded",
                "reservation_id": str(reservation.id),
                "occurred_at": datetime.now(UTC).isoformat(),
                "payment_id": str(charge.id),
                "amount_cents": charge.amount_cents,
            },
        )
    async with UnitOfWork() as uow:
        await relay.publish_pending_events(uow, relay.make_producer())

    dead_letter = booking_consumer.make_dead_letter(relay.make_producer())
    for _ in range(CONSUME_TIMEOUT_POLLS):
        await booking_consumer._process_one(kafka_consumer, dead_letter)
        if await _refunds(payment_db_session, reservation):
            break

    assert await _refunds(payment_db_session, reservation) == [reservation.total_cents]
