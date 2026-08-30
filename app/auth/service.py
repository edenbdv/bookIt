import uuid

from app.auth import token_store
from app.auth.models import User
from app.auth.schemas import Token, UserCreate
from app.auth.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    new_id,
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


async def issue_tokens(user_id: uuid.UUID) -> Token:
    """Start a new login session. Every login gets its own sid, which is what
    makes per-device logout possible -- one session dying leaves the others
    alone (docs/auth/refresh-token-rotation-basics.md)."""
    session_id = new_id()
    jti = new_id()
    tokens = Token(
        access_token=create_access_token(user_id, session_id),
        refresh_token=create_refresh_token(user_id, session_id, jti),
    )
    await token_store.start_session(str(user_id), session_id, jti)
    return tokens


def _decode_refresh(refresh_token: str) -> dict:
    try:
        payload = decode_token(refresh_token)
    except TokenError as exc:
        raise InvalidTokenError from exc

    # rejects an access token presented here -- the two are structurally
    # identical JWTs otherwise, only the "type" claim tells them apart
    # (security.py's _create_token)
    if payload.get("type") != "refresh":
        raise InvalidTokenError

    # a token minted before BOOK-78 carries neither claim, so it has no session
    # to check against and can't be rotated -- rejected rather than waved
    # through, which just means those sessions re-login once
    if not payload.get("jti") or not payload.get("sid"):
        raise InvalidTokenError

    return payload


async def refresh_tokens(uow: UnitOfWork, refresh_token: str) -> Token:
    payload = _decode_refresh(refresh_token)

    user = await uow.users.get_by_id(uuid.UUID(payload["sub"]))
    # covers a deleted/no-longer-existing user holding an otherwise-valid,
    # unexpired refresh token -- shouldn't still be able to mint new tokens
    if user is None:
        raise InvalidTokenError

    session_id = payload["sid"]
    # not necessarily the jti we end up minting with: inside the grace window
    # rotate() hands back the session's *current* jti instead, so two tabs
    # refreshing at once converge on one token rather than forking the chain
    minted_jti = await token_store.rotate(session_id, payload["jti"], new_id())
    # rotate() has already destroyed the session if this was a reuse. The 401
    # the caller turns this into is deliberately identical to every other
    # failure: telling someone "that token was already used" confirms to a
    # thief that the token they hold is genuine.
    if minted_jti is None:
        raise InvalidTokenError

    return Token(
        access_token=create_access_token(user.id, session_id),
        refresh_token=create_refresh_token(user.id, session_id, minted_jti),
    )


async def logout(refresh_token: str) -> None:
    """End one session. Its refresh token dies now rather than in up to 7 days.

    The access token issued alongside it stays valid until it expires (minutes)
    -- see rotation-basics.md section 4 for why that's the accepted tradeoff.
    """
    payload = _decode_refresh(refresh_token)
    await token_store.revoke_session(payload["sub"], payload["sid"])


async def logout_all(user_id: uuid.UUID) -> int:
    """End every session for this user -- the "sign out everywhere" action, and
    what a password change should call. Returns how many were ended."""
    return await token_store.revoke_all_sessions(str(user_id))
