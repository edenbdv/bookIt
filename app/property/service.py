import uuid
from datetime import UTC, datetime

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


async def delete_property(uow: UnitOfWork, id: uuid.UUID) -> bool:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return False
    # deliberately NOT uow.properties.delete(property_) -- that's the
    # inherited BaseRepository hard delete, which core/README.md's
    # "repository.py" section explicitly says won't stop misuse; soft
    # delete is service.py's job specifically because it's a business
    # rule, not data access. deleted_at has no DB default (unlike
    # created_at) -- it's meant to be set here, on delist, not by Postgres
    property_.deleted_at = datetime.now(UTC)
    return True
