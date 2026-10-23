"""BOOK-66: GET /internal/reservations/{id} -- only a valid service token from
an allowed caller gets through, and a service token works nowhere else
(docs/auth/service-token-basics.md §6). Through HTTP, so require_service and
the router are both in the loop.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from tests.integration.helpers import auth_headers, make_pending_reservation


def _service_token(
    sub: str = "payment",
    *,
    type_: str = "service",
    secret: str | None = None,
    expires_in: timedelta = timedelta(minutes=5),
) -> str:
    # minted here the way Payment will -- Booking's code never creates one
    settings = get_settings()
    return jwt.encode(
        {"sub": sub, "type": type_, "exp": datetime.now(UTC) + expires_in},
        secret or settings.service_token_secret,
        algorithm=settings.jwt_algorithm,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
async def test_payment_reads_exactly_the_fields_it_needs(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    reservation, guest = await make_pending_reservation(db_session)

    response = await api_client.get(
        f"/internal/reservations/{reservation.id}", headers=_bearer(_service_token())
    )

    assert response.status_code == 200
    # the whole contract -- nothing else leaves Booking
    assert response.json() == {
        "id": str(reservation.id),
        "guest_id": str(guest.id),
        "status": "pending",
        "total_cents": 40000,
    }


@pytest.mark.anyio
async def test_an_unknown_reservation_is_404(api_client: AsyncClient) -> None:
    response = await api_client.get(
        f"/internal/reservations/{uuid.uuid4()}", headers=_bearer(_service_token())
    )

    assert response.status_code == 404


# rule 1 (§6): any logged-in guest could otherwise read any reservation
@pytest.mark.anyio
async def test_a_guest_token_is_rejected(api_client: AsyncClient, db_session: AsyncSession) -> None:
    reservation, guest = await make_pending_reservation(db_session)

    response = await api_client.get(
        f"/internal/reservations/{reservation.id}", headers=auth_headers(guest)
    )

    assert response.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_service_token("notification"), id="a service not on the allow-list"),
        pytest.param(_service_token(type_="access"), id="right secret, wrong type"),
        pytest.param(_service_token(secret="some-other-secret"), id="wrong secret"),
        pytest.param(_service_token(expires_in=timedelta(minutes=-1)), id="expired"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
async def test_an_invalid_service_token_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, token: str
) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    response = await api_client.get(
        f"/internal/reservations/{reservation.id}", headers=_bearer(token)
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_no_token_is_rejected(api_client: AsyncClient, db_session: AsyncSession) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    response = await api_client.get(f"/internal/reservations/{reservation.id}")

    # HTTPBearer's own rejection, before require_service runs
    assert response.status_code == 401


# rule 2 (§6): a service token is no guest -- guest routes reject it
@pytest.mark.anyio
async def test_a_service_token_is_rejected_on_guest_routes(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    response = await api_client.get(
        f"/reservations/{reservation.id}", headers=_bearer(_service_token())
    )

    assert response.status_code == 401
