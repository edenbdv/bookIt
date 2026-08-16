import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReviewCreate(BaseModel):
    reservation_id: uuid.UUID
    # matches the DB's CheckConstraint("rating BETWEEN 1 AND 5"), but
    # enforced here too so a bad rating gets a clean 422 before the
    # request ever reaches the database (pydantic-schemas-basics.md
    # section 2's "validation with a good error message" point)
    rating: int = Field(ge=1, le=5)
    comment: str
    # no id, no author_id, no created_at: author_id comes from the
    # authenticated current user, not the client -- whether that user is
    # reviewing as guest or host is inferred server-side from the
    # reservation (see the comment on Review.author_id in models.py)


class ReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    reservation_id: uuid.UUID
    author_id: uuid.UUID
    rating: int
    comment: str
    created_at: datetime


class ReviewUpdate(BaseModel):
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = None
