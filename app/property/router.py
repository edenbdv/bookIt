import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.unit_of_work import UnitOfWork, get_uow
from app.property import service
from app.property.models import Property, PropertyPhoto
from app.property.schemas import (
    PropertyCreate,
    PropertyDetailRead,
    PropertyListRead,
    PropertyPhotoCreate,
    PropertyPhotoRead,
    PropertyPhotoReorder,
    PropertyRead,
    PropertyUpdate,
)

router = APIRouter(prefix="/properties", tags=["properties"])


@router.post("", response_model=PropertyRead, status_code=status.HTTP_201_CREATED)
async def create_property(
    data: PropertyCreate,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> Property:
    return await service.create_property(uow, data, current_user.id)


@router.get("/{property_id}", response_model=PropertyDetailRead)
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


@router.get("", response_model=list[PropertyListRead])
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
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> Property:
    try:
        property_ = await service.update_property(uow, property_id, data, current_user.id)
    except service.NotPropertyOwnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this property's host may update it",
        ) from exc
    if property_ is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    return property_


@router.delete("/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_property(
    property_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    # status_code=204 (the status LINE) and -> None (no response_model, so
    # no BODY) have to agree -- HTTP requires a 204 response to have no
    # body at all. Returning the deleted Property here instead would be
    # wrong for this status code, not just unnecessary.
    try:
        deleted = await service.delete_property(uow, property_id, current_user.id)
    except service.NotPropertyOwnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this property's host may delete it",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")


@router.post(
    "/{property_id}/photos", response_model=PropertyPhotoRead, status_code=status.HTTP_201_CREATED
)
async def add_photo(
    property_id: uuid.UUID,
    data: PropertyPhotoCreate,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> PropertyPhoto:
    try:
        return await service.add_photo(uow, property_id, data, current_user.id)
    except service.PropertyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Property not found"
        ) from exc
    except service.NotPropertyOwnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this property's host may add photos",
        ) from exc


@router.delete("/{property_id}/photos/{photo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_photo(
    property_id: uuid.UUID,
    photo_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> None:
    try:
        await service.remove_photo(uow, property_id, photo_id, current_user.id)
    except service.PhotoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found"
        ) from exc
    except service.NotPropertyOwnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this property's host may remove photos",
        ) from exc


@router.put("/{property_id}/photos", response_model=list[PropertyPhotoRead])
async def reorder_photos(
    property_id: uuid.UUID,
    data: PropertyPhotoReorder,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> list[PropertyPhoto]:
    try:
        return await service.reorder_photos(uow, property_id, data, current_user.id)
    except service.PropertyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Property not found"
        ) from exc
    except service.NotPropertyOwnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this property's host may reorder photos",
        ) from exc
    except service.PhotoOrderMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="photo_ids must exactly match this property's current photos",
        ) from exc
