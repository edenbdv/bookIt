import uuid

from app.core.unit_of_work import UnitOfWork
from app.property.models import Property
from app.property.schemas import PropertyCreate


async def create_property(uow: UnitOfWork, data: PropertyCreate, host_id: uuid.UUID) -> Property:
    property_ = Property(host_id=host_id, **data.model_dump())
    uow.properties.add(property_)
    # flush, not commit: populates the server-generated created_at so the
    # response can include it -- commit happens once at request end, in
    # get_uow()'s __aexit__ (docs/domain-layer/unit-of-work-basics.md)
    await uow.session.flush()
    return property_


async def get_property(uow: UnitOfWork, id: uuid.UUID) -> Property | None:
    return await uow.properties.get_active_by_id(id)


async def list_properties(
    uow: UnitOfWork,
    limit: int,
    offset: int,
    city: str | None = None,
    max_guests: int | None = None,
    min_price_cents: int | None = None,
    max_price_cents: int | None = None,
) -> list[Property]:
    return await uow.properties.list_filtered(
        limit=limit,
        offset=offset,
        city=city,
        max_guests=max_guests,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
    )
