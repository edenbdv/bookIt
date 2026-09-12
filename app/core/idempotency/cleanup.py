"""Delete idempotency keys past their own expires_at -- deferred from
BOOK-36, same shape as core/outbox/cleanup.py. Nothing currently checks
expires_at when handling a request (a key found COMPLETED is honored
regardless of age); this only bounds how long a row sticks around, not
whether it's still treated as valid while it exists.
"""

from datetime import UTC, datetime

from sqlalchemy import delete

from app.core.db.unit_of_work import UnitOfWork
from app.core.idempotency.models import IdempotencyKey


async def delete_expired_keys(uow: UnitOfWork) -> None:
    await uow.session.execute(
        delete(IdempotencyKey).where(IdempotencyKey.expires_at < datetime.now(UTC))
    )
