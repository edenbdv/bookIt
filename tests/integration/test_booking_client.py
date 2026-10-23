"""BOOK-66: Payment's booking_client against Booking's real /internal endpoint,
in-process (booking_in_process) -- the HTTP call, the service token and
Booking's require_service all in the loop, no server running.
"""

import uuid
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.payment import booking_client
from app.payment.booking_client import (
    BookingReservationStatus,
    BookingUnavailableError,
    ReservationInfo,
)
from tests.integration.helpers import make_pending_reservation


@pytest.mark.anyio
async def test_reads_the_reservation_payment_needs(
    booking_in_process: None, db_session: AsyncSession
) -> None:
    reservation, guest = await make_pending_reservation(db_session)

    info = await booking_client.get_reservation(reservation.id)

    assert info == ReservationInfo(
        id=reservation.id,
        guest_id=guest.id,
        status=BookingReservationStatus.PENDING,
        total_cents=40000,
    )


@pytest.mark.anyio
async def test_an_unknown_reservation_is_none(booking_in_process: None) -> None:
    assert await booking_client.get_reservation(uuid.uuid4()) is None


# Payment's secret doesn't match Booking's -> Booking says 401. That's Booking
# refusing us, not "no such reservation" -- it must not look like a 404.
@pytest.mark.anyio
async def test_a_rejected_token_is_unavailable_not_none(
    booking_in_process: None, db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    with (
        patch.object(booking_client, "_service_token", return_value="not-a-real-token"),
        pytest.raises(BookingUnavailableError, match="401"),
    ):
        await booking_client.get_reservation(reservation.id)


# Booking down or slow -> BookingUnavailableError, which the router turns
# into a 503 "try again", not a 500
@pytest.mark.anyio
async def test_booking_unreachable_is_unavailable() -> None:
    def unreachable() -> httpx.AsyncClient:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        return httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://booking")

    with (
        patch.object(booking_client, "_client", unreachable),
        pytest.raises(BookingUnavailableError),
    ):
        await booking_client.get_reservation(uuid.uuid4())
