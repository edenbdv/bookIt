import uuid

from app.auth.models import User
from app.auth.schemas import Token, UserCreate
from app.auth.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.core.unit_of_work import UnitOfWork


class EmailAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


async def signup(uow: UnitOfWork, data: UserCreate) -> User:
    existing = await uow.users.get_by_email(data.email)
    if existing is not None:
        raise EmailAlreadyExistsError

    # role isn't part of UserCreate (schemas.py) -- always starts at the
    # model's default (UserRole.USER); granting ADMIN is a separate action
    user = User(
        email=data.email,
        display_name=data.display_name,
        hashed_password=hash_password(data.password),
    )
    uow.users.add(user)
    # flush, not commit: populates the server-generated created_at so the
    # response can include it -- commit happens once at request end, in
    # get_uow()'s __aexit__ (docs/domain-layer/unit-of-work-basics.md)
    await uow.session.flush()
    return user


async def login(uow: UnitOfWork, email: str, password: str) -> User:
    user = await uow.users.get_by_email(email)
    # same error for "no such email" and "wrong password" -- confirming
    # which one it was would tell an attacker whether a given email is
    # registered at all
    if user is None or not verify_password(password, user.hashed_password):
        raise InvalidCredentialsError
    return user


def issue_tokens(user_id: uuid.UUID) -> Token:
    return Token(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
    )


async def refresh_tokens(uow: UnitOfWork, refresh_token: str) -> Token:
    try:
        payload = decode_token(refresh_token)
    except TokenError as exc:
        raise InvalidTokenError from exc

    # rejects an access token presented here -- the two are structurally
    # identical JWTs otherwise, only the "type" claim tells them apart
    # (security.py's _create_token)
    if payload.get("type") != "refresh":
        raise InvalidTokenError

    user = await uow.users.get_by_id(uuid.UUID(payload["sub"]))
    # covers a deleted/no-longer-existing user holding an otherwise-valid,
    # unexpired refresh token -- shouldn't still be able to mint new tokens
    if user is None:
        raise InvalidTokenError

    return issue_tokens(user.id)
