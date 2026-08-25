import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PropertyCreate(BaseModel):
    title: str
    description: str
    city: str
    country: str
    # matches the DB's CheckConstraint("max_guests > 0", "price_non_negative")
    # -- rejected here with a clean 422 before the request reaches the
    # database, same reasoning as ReviewCreate.rating
    max_guests: int = Field(gt=0)
    price_per_night_cents: int = Field(ge=0)
    # no id, no host_id, no created_at, no deleted_at -- id/created_at are
    # server-generated, host_id comes from the authenticated current user,
    # deleted_at is an internal soft-delete detail with its own delist action


class PropertyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    host_id: uuid.UUID
    title: str
    description: str
    city: str
    country: str
    max_guests: int
    price_per_night_cents: int
    created_at: datetime


class PropertyUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    city: str | None = None
    country: str | None = None
    max_guests: int | None = Field(default=None, gt=0)
    price_per_night_cents: int | None = Field(default=None, ge=0)


class PropertyPhotoCreate(BaseModel):
    url: str
    # no position -- adding a photo appends it to the end (server-computed);
    # changing order is reorder's job, a separate operation, not something
    # a client picks when adding a new photo
    # no id, no property_id, no created_at -- server-generated/from the URL path


class PropertyPhotoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    property_id: uuid.UUID
    url: str
    position: int
    created_at: datetime


class PropertyRatingRead(BaseModel):
    """The review aggregate shared by the list and detail responses (BOOK-77).

    Not on PropertyRead itself: create/update don't compute it, so it would
    always be null there -- same reasoning as cover_photo.
    """

    # None (not 0.0) when a property has no reviews yet -- "unrated" and
    # "rated zero" are different things, and the UI shows them differently
    rating_avg: float | None
    review_count: int


class PropertyListRead(PropertyRead, PropertyRatingRead):
    # only the list endpoint computes this (one DISTINCT ON query per page,
    # docs/db/sql-basics.md#7) -- create/update don't, so it doesn't belong
    # on the shared PropertyRead they use (would always show null there)
    cover_photo: PropertyPhotoRead | None


class PropertyDetailRead(PropertyRead, PropertyRatingRead):
    # the full gallery -- only get_property (single fetch) computes this;
    # PropertyRead stays photo-less for create/update, PropertyListRead
    # carries just a cover_photo for the list endpoint (see its comment)
    #
    photos: list[PropertyPhotoRead]


class PropertyPhotoReorder(BaseModel):
    # the complete new order, not one photo's target position -- position i
    # in this list becomes photo i's new `position`. Whether this list is
    # exactly the property's current photo set (no missing/extra ids) needs
    # a DB lookup, so that check lives in service.py, not here
    photo_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def no_duplicate_ids(self) -> "PropertyPhotoReorder":
        if len(set(self.photo_ids)) != len(self.photo_ids):
            raise ValueError("photo_ids must not contain duplicates")
        return self
