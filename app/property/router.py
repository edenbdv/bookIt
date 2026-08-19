import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.unit_of_work import UnitOfWork, get_uow
from app.property import service
from app.property.models import Property
from app.property.schemas import PropertyCreate, PropertyRead, PropertyUpdate

router = APIRouter(prefix="/properties", tags=["properties"])


@router.post("", response_model=PropertyRead, status_code=status.HTTP_201_CREATED)
async def create_property(
    data: PropertyCreate,
    # SECURITY GAP, not just "temporary": nothing verifies the caller is
    # actually this host -- anyone who knows/guesses a host_id can create
    # listings under it. Acceptable for local dev only (e.g. seed.py's host
    # UUIDs); do not expose this build publicly before BOOK-5 (auth) replaces
    # this with a host_id derived from Depends(get_current_user). See
    # PropertyCreate's own comment for why host_id isn't part of the body
    # schema instead.
    host_id: uuid.UUID,
    uow: UnitOfWork = Depends(get_uow),
) -> Property:
    return await service.create_property(uow, data, host_id)


@router.get("/{property_id}", response_model=PropertyRead)
async def get_property(
    property_id: uuid.UUID,
    uow: UnitOfWork = Depends(get_uow),
) -> Property:
    # service.py stays HTTP-agnostic and returns None -- the 404 mapping
    # happens here, not there (docs/domain-layer/router-service-basics.md)
    property_ = await service.get_property(uow, property_id)
    if property_ is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    return property_


@router.get("", response_model=list[PropertyRead])
async def list_properties(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    city: str | None = None,
    guests: int | None = Query(default=None, gt=0),
    min_price_cents: int | None = Query(default=None, ge=0),
    max_price_cents: int | None = Query(default=None, ge=0),
    uow: UnitOfWork = Depends(get_uow),
) -> list[Property]:
    return await service.list_properties(
        uow, limit, offset, city, guests, min_price_cents, max_price_cents
    )


@router.patch("/{property_id}", response_model=PropertyRead)
async def update_property(
    property_id: uuid.UUID,
    data: PropertyUpdate,
    # same SECURITY GAP as create_property: nothing verifies the caller is
    # this property's host, or is authenticated at all -- anyone can PATCH
    # any property. Acceptable for local dev only; BOOK-5 needs to add a
    # host_id == current_user.id check here before this build goes public.
    uow: UnitOfWork = Depends(get_uow),
) -> Property:
    property_ = await service.update_property(uow, property_id, data)
    if property_ is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    return property_


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property(
    property_id: uuid.UUID,
    # same SECURITY GAP as update_property/create_property -- see those
    # comments
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    # status_code=204 (the status LINE) and -> None (no response_model, so
    # no BODY) have to agree -- HTTP requires a 204 response to have no
    # body at all. Returning the deleted Property here instead would be
    # wrong for this status code, not just unnecessary.
    deleted = await service.delete_property(uow, property_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
