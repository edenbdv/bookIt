from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.property.models import Property


class PropertyRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, property_id: UUID) -> Property | None:
        return await self.session.get(Property, property_id)

    async def list_by_city(self, city: str, limit: int, offset: int) -> list[Property]:
        stmt = select(Property).where(Property.city == city).limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return list(result)

    def add(self, property: Property) -> None:
        self.session.add(property)
