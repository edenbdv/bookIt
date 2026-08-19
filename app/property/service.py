import uuid

from app.core.unit_of_work import UnitOfWork
from app.property.models import Property
from app.property.schemas import PropertyCreate, PropertyUpdate


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
    guests: int | None = None,
    min_price_cents: int | None = None,
    max_price_cents: int | None = None,
) -> list[Property]:
    return await uow.properties.list_filtered(
        limit=limit,
        offset=offset,
        city=city,
        guests=guests,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
    )


async def update_property(uow: UnitOfWork, id: uuid.UUID, data: PropertyUpdate) -> Property | None:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return None
    # exclude_unset=True: only include fields the client actually sent,
    # not every field with omitted ones defaulting to None. Without it,
    # {"title": "x"} would dump as {"title": "x", "description": None,
    # ...} and the loop below would wipe every field the client didn't
    # mention -- exclude_unset is what makes this a true partial update
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(property_, field, value)
    return property_
