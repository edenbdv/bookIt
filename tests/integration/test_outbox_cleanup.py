"""BOOK-64 regression test: the daily outbox cleanup deletes an old published
event even after Notification has handled it.

Before step 4a, processed_notification_events.event_id had a foreign key (no
ON DELETE) into outbox_events -- so this DELETE failed with a foreign key
violation as soon as Notification had processed any event older than the
retention window, and the cleanup job failed every day after that. Since
step 4 the Inbox lives in Notification's own database, so Booking's cleanup
can't even see it -- this pins that down.
"""

from datetime import UTC, datetime, timedelta

import pytest
from notification.models import ProcessedNotificationEvent
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox.cleanup import OUTBOX_RETENTION_DAYS, delete_published_events
from app.core.outbox.models import OutboxEvent
from app.reservation import service as reservation_service
from tests.integration.helpers import make_pending_reservation


@pytest.mark.anyio
async def test_cleanup_deletes_an_old_event_notification_already_handled(
    app_db: None, db_session: AsyncSession, notification_db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:
        await reservation_service.confirm_reservation(uow, reservation.id)
    event_id = await db_session.scalar(
        select(OutboxEvent.id).where(OutboxEvent.reservation_id == reservation.id)
    )

    # published long ago (Booking's DB)...
    long_ago = datetime.now(UTC) - timedelta(days=OUTBOX_RETENTION_DAYS + 1)
    await db_session.execute(
        update(OutboxEvent).where(OutboxEvent.id == event_id).values(published_at=long_ago)
    )
    await db_session.commit()
    # ...and Notification already sent its email (Notification's own DB)
    notification_db_session.add(ProcessedNotificationEvent(event_id=event_id))
    await notification_db_session.commit()

    async with UnitOfWork() as uow:
        await delete_published_events(uow)  # raised IntegrityError before 4a

    assert await db_session.scalar(select(func.count()).select_from(OutboxEvent)) == 0
    # Notification's Inbox row is its own business, in its own database -- it stays
    assert (
        await notification_db_session.scalar(
            select(func.count()).select_from(ProcessedNotificationEvent)
        )
        == 1
    )
