import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


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
