"""BOOK-65: Payment's two cleanup jobs against a real Postgres -- they must delete
exactly what's old enough, and never an unpublished event (the relay still owes it)
or a live idempotency key."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from payment import cleanup
from payment.db import PaymentUnitOfWork
from payment.models import (
    PaymentEventType,
    PaymentIdempotencyKey,
    PaymentIdempotencyKeyStatus,
    PaymentOutboxEvent,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime.now(UTC)


def _event(published_at: datetime | None) -> PaymentOutboxEvent:
    return PaymentOutboxEvent(
        event_type=PaymentEventType.PAYMENT_SUCCEEDED,
        reservation_id=uuid.uuid4(),
        payload={},
        published_at=published_at,
    )


@pytest.mark.anyio
async def test_only_old_published_events_are_deleted(
    app_db: None, payment_db_session: AsyncSession
) -> None:
    old = _event(NOW - timedelta(days=cleanup.OUTBOX_RETENTION_DAYS + 1))
    recent = _event(NOW - timedelta(days=1))
    unpublished = _event(None)  # however old -- the relay still has to send it
    payment_db_session.add_all([old, recent, unpublished])
    await payment_db_session.commit()

    async with PaymentUnitOfWork() as uow:
        deleted = await cleanup.delete_published_events(uow)

    assert deleted == 1
    left = set(await payment_db_session.scalars(select(PaymentOutboxEvent.id)))
    assert left == {recent.id, unpublished.id}


@pytest.mark.anyio
async def test_only_expired_keys_are_deleted(
    app_db: None, payment_db_session: AsyncSession
) -> None:
    payment_db_session.add_all(
        [
            PaymentIdempotencyKey(
                key="expired",
                status=PaymentIdempotencyKeyStatus.COMPLETED,
                expires_at=NOW - timedelta(minutes=1),
            ),
            PaymentIdempotencyKey(
                key="live",
                status=PaymentIdempotencyKeyStatus.COMPLETED,
                expires_at=NOW + timedelta(hours=23),
            ),
        ]
    )
    await payment_db_session.commit()

    async with PaymentUnitOfWork() as uow:
        deleted = await cleanup.delete_expired_keys(uow)

    assert deleted == 1
    assert list(await payment_db_session.scalars(select(PaymentIdempotencyKey.key))) == ["live"]
