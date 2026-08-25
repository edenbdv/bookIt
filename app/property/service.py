import uuid
from datetime import UTC, datetime

from app.core.unit_of_work import UnitOfWork
from app.property.models import Property, PropertyPhoto
from app.property.schemas import (
    PropertyCreate,
    PropertyPhotoCreate,
    PropertyPhotoReorder,
    PropertyUpdate,
)


class PropertyNotFoundError(Exception):
    pass


class PhotoNotFoundError(Exception):
    pass


class PhotoOrderMismatchError(Exception):
    pass


class NotPropertyOwnerError(Exception):
    pass


async def create_property(uow: UnitOfWork, data: PropertyCreate, host_id: uuid.UUID) -> Property:
    property_ = Property(host_id=host_id, **data.model_dump())
    uow.properties.add(property_)
    # flush, not commit: populates the server-generated created_at so the
    # response can include it -- commit happens once at request end, in
    # get_uow()'s __aexit__ (docs/domain-layer/unit-of-work-basics.md)
    await uow.session.flush()
    return property_


async def get_property(uow: UnitOfWork, id: uuid.UUID) -> Property | None:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return None
    # not a mapped column/relationship -- attached here purely so
    # PropertyDetailRead's from_attributes=True can read it (same technique
    # as list_properties' cover_photo above); never persisted
    property_.photos = await uow.property_photos.list_by_property_id(id)
    return property_


async def list_properties(
    uow: UnitOfWork,
    limit: int,
    offset: int,
    city: str | None = None,
    guests: int | None = None,
    min_price_cents: int | None = None,
    max_price_cents: int | None = None,
) -> list[Property]:
    properties = await uow.properties.list_filtered(
        limit=limit,
        offset=offset,
        city=city,
        guests=guests,
        min_price_cents=min_price_cents,
        max_price_cents=max_price_cents,
    )

    property_ids = [property_.id for property_ in properties]
    cover_photos = await uow.property_photos.list_cover_photos(property_ids)
    cover_photo_by_property_id = {photo.property_id: photo for photo in cover_photos}
    for property_ in properties:
        # not a mapped column/relationship -- attached here purely so
        # PropertyListRead's from_attributes=True can read it
        # (docs/domain-layer/router-service-basics.md); never persisted
        property_.cover_photo = cover_photo_by_property_id.get(property_.id)

    return properties


async def list_cities(uow: UnitOfWork) -> list[str]:
    return await uow.properties.list_distinct_cities()


async def update_property(
    uow: UnitOfWork, id: uuid.UUID, data: PropertyUpdate, current_user_id: uuid.UUID
) -> Property | None:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return None
    # checked after existence, not before -- 404 for a nonexistent property
    # takes priority over 403 for a real one you don't own, same order as
    # review/service.py's ReservationNotFoundError-before-NotAParticipantError
    if property_.host_id != current_user_id:
        raise NotPropertyOwnerError
    # exclude_unset=True: only include fields the client actually sent,
    # not every field with omitted ones defaulting to None. Without it,
    # {"title": "x"} would dump as {"title": "x", "description": None,
    # ...} and the loop below would wipe every field the client didn't
    # mention -- exclude_unset is what makes this a true partial update
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(property_, field, value)
    return property_


async def delete_property(uow: UnitOfWork, id: uuid.UUID, current_user_id: uuid.UUID) -> bool:
    property_ = await uow.properties.get_active_by_id(id)
    if property_ is None:
        return False
    # same order as update_property: 404 (via False, below) takes priority
    # over 403 for a real property you don't own
    if property_.host_id != current_user_id:
        raise NotPropertyOwnerError
    # deliberately NOT uow.properties.delete(property_) -- that's the
    # inherited BaseRepository hard delete, which core/README.md's
    # "repository.py" section explicitly says won't stop misuse; soft
    # delete is service.py's job specifically because it's a business
    # rule, not data access. deleted_at has no DB default (unlike
    # created_at) -- it's meant to be set here, on delist, not by Postgres
    property_.deleted_at = datetime.now(UTC)
    return True


async def add_photo(
    uow: UnitOfWork, property_id: uuid.UUID, data: PropertyPhotoCreate, current_user_id: uuid.UUID
) -> PropertyPhoto:
    property_ = await uow.properties.get_active_by_id(property_id)
    if property_ is None:
        raise PropertyNotFoundError
    if property_.host_id != current_user_id:
        raise NotPropertyOwnerError

    existing = await uow.property_photos.list_by_property_id(property_id)
    # max(...) + 1, not len(existing): a prior remove can leave gaps in
    # position (e.g. 0, 2 after removing 1) -- len() could collide with an
    # existing position in that case, max()+1 never does
    next_position = max((photo.position for photo in existing), default=-1) + 1
    photo = PropertyPhoto(property_id=property_id, position=next_position, **data.model_dump())
    uow.property_photos.add(photo)
    await uow.session.flush()
    return photo


async def remove_photo(
    uow: UnitOfWork, property_id: uuid.UUID, photo_id: uuid.UUID, current_user_id: uuid.UUID
) -> None:
    # no property-exists/active check: a bogus property_id can't match a
    # real photo anyway, and cleanup should work even on a delisted listing
    photo = await uow.property_photos.get_by_id(photo_id)
    # property_id match prevents deleting a real photo via someone else's
    # property URL -- without it, property_id in the URL is decorative
    if photo is None or photo.property_id != property_id:
        raise PhotoNotFoundError

    # get_by_id, not get_active_by_id -- ownership still applies to a
    # delisted property's photos, same "works even when delisted" reasoning
    # as the photo lookup above. The photo's own FK guarantees this property
    # row exists, so no None check is needed here.
    property_ = await uow.properties.get_by_id(property_id)
    if property_.host_id != current_user_id:
        raise NotPropertyOwnerError

    await uow.property_photos.delete(photo)


async def reorder_photos(
    uow: UnitOfWork, property_id: uuid.UUID, data: PropertyPhotoReorder, current_user_id: uuid.UUID
) -> list[PropertyPhoto]:
    property_ = await uow.properties.get_active_by_id(property_id)
    if property_ is None:
        raise PropertyNotFoundError
    if property_.host_id != current_user_id:
        raise NotPropertyOwnerError

    existing = await uow.property_photos.list_by_property_id(property_id)
    photos_by_id = {photo.id: photo for photo in existing}
    # the submitted list must be an exact permutation of the current photo
    # set -- not checkable in the schema (needs a DB lookup), so it's here
    if set(data.photo_ids) != set(photos_by_id):
        raise PhotoOrderMismatchError

    for position, photo_id in enumerate(data.photo_ids):
        photos_by_id[photo_id].position = position

    # return in the new order, not DB-load order -- confirms the applied
    # order back to the caller without needing a separate GET
    return [photos_by_id[photo_id] for photo_id in data.photo_ids]
