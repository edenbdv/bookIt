from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import service
from app.auth.models import User
from app.auth.schemas import RefreshRequest, Token, UserCreate, UserLogin, UserRead
from app.core.unit_of_work import UnitOfWork, get_uow

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def signup(
    data: UserCreate,
    uow: UnitOfWork = Depends(get_uow),
) -> User:
    try:
        return await service.signup(uow, data)
    except service.EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc


@router.post("/login", response_model=Token)
async def login(
    data: UserLogin,
    uow: UnitOfWork = Depends(get_uow),
) -> Token:
    try:
        user = await service.login(uow, data.email, data.password)
    except service.InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        ) from exc
    return service.issue_tokens(user.id)


@router.post("/refresh", response_model=Token)
async def refresh(
    data: RefreshRequest,
    uow: UnitOfWork = Depends(get_uow),
) -> Token:
    try:
        return await service.refresh_tokens(uow, data.refresh_token)
    except service.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        ) from exc
