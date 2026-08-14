from sqlalchemy import select

from app.core.repository import BaseRepository
from app.property.models import Property


class PropertyRepository(BaseRepository[Property]):
    model = Property

    async def list_by_city(self, city: str, limit: int, offset: int) -> list[Property]:
        # order_by is required for LIMIT/OFFSET to paginate consistently
        # (docs/db/sql-basics.md#5); id is a tiebreaker since price alone
        # isn't unique. Exposing other sort options is BOOK-29's concern.
        stmt = (
            select(Property)
            .where(Property.city == city)
            .order_by(Property.price_per_night_cents.asc(), Property.id.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.scalars(stmt)
        return list(result)
