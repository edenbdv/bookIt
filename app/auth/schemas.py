import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.auth.models import UserRole


class UserCreate(BaseModel):
    email: EmailStr
    # required (min_length=1 rejects ""), unlike the nullable column on the
    # model -- every NEW account must have a name, pre-existing rows keep
    # NULL. This is what a public review is attributed to instead of email.
    display_name: str = Field(min_length=1)
    # plain password from the client; service.py hashes it into
    # User.hashed_password before it ever reaches the repository
    password: str = Field(min_length=8)
    # no id, no role, no created_at -- server-generated. role always
    # starts at UserRole.USER; granting ADMIN is a separate admin action,
    # not something a signup request can request for itself


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    role: UserRole
    created_at: datetime
    # no hashed_password -- see pydantic-schemas-basics.md section 2


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    # same shape as RefreshRequest today, kept separate because they're
    # different contracts: this token is being surrendered, not exchanged.
    # Logging out by refresh token (not access token) is what lets the server
    # kill the long-lived half of the pair -- BOOK-78
    refresh_token: str


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    # no password here -- changing a password is a security-sensitive
    # action (should require confirming the current password), so it
    # belongs on its own dedicated endpoint/schema, not a generic PATCH
