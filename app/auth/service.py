from app.auth.models import User
from app.auth.schemas import UserCreate
from app.auth.security import hash_password, verify_password
from app.core.unit_of_work import UnitOfWork


class EmailAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


async def signup(uow: UnitOfWork, data: UserCreate) -> User:
    existing = await uow.users.get_by_email(data.email)
    if existing is not None:
        raise EmailAlreadyExistsError

    # role isn't part of UserCreate (schemas.py) -- always starts at the
    # model's default (UserRole.USER); granting ADMIN is a separate action
    user = User(email=data.email, hashed_password=hash_password(data.password))
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
