"""BOOK-65: Booking's outbox relay must mark a row published only once Kafka
confirmed it -- the same bug fixed in Payment's relay
(test_payment_relay.py). The real-Kafka happy path is already covered by
test_notification_pipeline.py.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox import relay
from app.core.outbox.models import OutboxEvent
from app.reservation import service as reservation_service
from tests.integration.helpers import make_pending_reservation


class _NeverConfirmsProducer:
    """Accepts messages but never calls their delivery callback -- what a
    real producer does while the topic is missing or the broker is slow or
    down: no success, no failure, just still waiting when flush() times out."""

    def __init__(self) -> None:
        self.produced = 0

    def produce(self, *_args: object, **_kwargs: object) -> None:
        self.produced += 1

    def flush(self, timeout: float) -> int:
        return self.produced  # all still waiting


@pytest.mark.anyio
async def test_an_unconfirmed_event_stays_unpublished_and_is_retried(
    app_db: None, db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:  # writes a booking_confirmed outbox row
        await reservation_service.confirm_reservation(uow, reservation.id)
    producer = _NeverConfirmsProducer()

    async with UnitOfWork() as uow:
        await relay.publish_pending_events(uow, producer)

    assert producer.produced == 1
    published_at = await db_session.scalar(select(OutboxEvent.published_at))
    assert published_at is None  # still pending -- the next run sends it again
