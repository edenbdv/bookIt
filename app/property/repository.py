from uuid import UUID

from sqlalchemy import select

from app.core.repository import BaseRepository
from app.property.models import Property


class PropertyRepository(BaseRepository[Property]):
    model = Property

    async def get_active_by_id(self, id: UUID) -> Property | None:
        # unlike the inherited get_by_id, excludes soft-deleted rows -- a
        # delisted property shouldn't be fetchable as if it were still live
        stmt = select(Property).where(Property.id == id, Property.deleted_at.is_(None))
        return await self.session.scalar(stmt)

    async def list_filtered(
        self,
        limit: int,
        offset: int,
        city: str | None = None,
        max_guests: int | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
    ) -> list[Property]:
        # every filter is optional -- omit one, it just doesn't narrow the
        # results. deleted_at IS NULL isn't optional: browsing/search should
        # never surface a delisted property (same reasoning as
        # get_active_by_id above).
        stmt = select(Property).where(Property.deleted_at.is_(None))
        if city is not None:
            # uses the existing ix_properties_city index (docs/db/indexes-basics.md)
            stmt = stmt.where(Property.city == city)
        if max_guests is not None:
            stmt = stmt.where(Property.max_guests >= max_guests)
        if min_price_cents is not None:
            stmt = stmt.where(Property.price_per_night_cents >= min_price_cents)
        if max_price_cents is not None:
            stmt = stmt.where(Property.price_per_night_cents <= max_price_cents)

        # order_by is required for LIMIT/OFFSET to paginate consistently
        # (docs/db/sql-basics.md#5); id is a tiebreaker since price alone
        # isn't unique. Exposing other sort options is BOOK-29's concern.
        stmt = stmt.order_by(Property.price_per_night_cents.asc(), Property.id.asc())
        stmt = stmt.limit(limit).offset(offset)

        result = await self.session.scalars(stmt)
        return list(result)
