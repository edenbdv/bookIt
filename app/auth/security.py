from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import bcrypt
import jwt

from app.core.config import get_settings


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


class TokenError(Exception):
    pass


def new_id() -> str:
    """A `jti` or `sid` value. Hex, not a UUID object -- these are only ever
    compared as strings and used as Redis key fragments (auth/token_store.py)."""
    return uuid4().hex


def _create_token(
    user_id: UUID,
    token_type: str,
    expires_delta: timedelta,
    session_id: str,
    jti: str,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        # lets decode_token's caller reject an access token presented where a
        # refresh token is required, and vice versa -- otherwise the two are
        # structurally identical and interchangeable, which they must not be
        "type": token_type,
        # BOOK-78. jti: this token's own identity, so one specific token can be
        # marked spent. sid: the login session it belongs to, so every token in
        # a chain can be killed together. Without a jti a token is
        # indistinguishable from any other with the same sub/exp, which is why
        # nothing could be revoked before -- see
        # docs/auth/refresh-token-rotation-basics.md
        "jti": jti,
        "sid": session_id,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: UUID, session_id: str) -> str:
    settings = get_settings()
    # its jti is generated but never recorded: access tokens are deliberately
    # NOT checked against Redis (that would be a round-trip on every single
    # request). They stay valid until they expire; revocation acts on the
    # refresh chain -- rotation-basics.md section 4 covers the tradeoff
    return _create_token(
        user_id,
        "access",
        timedelta(minutes=settings.access_token_expire_minutes),
        session_id,
        new_id(),
    )


def create_refresh_token(user_id: UUID, session_id: str, jti: str) -> str:
    settings = get_settings()
    # jti passed in, not generated here: the caller has to record the same
    # value in the token store, and generating it here would mean decoding
    # the token again just to find out what it was
    return _create_token(
        user_id,
        "refresh",
        timedelta(days=settings.refresh_token_expire_days),
        session_id,
        jti,
    )


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        # covers expiry, bad signature, malformed token -- all indistinguishable
        # to the caller, same reasoning as InvalidCredentialsError in service.py
        raise TokenError from exc
