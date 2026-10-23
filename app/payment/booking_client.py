"""BOOK-66: Payment's one sync call to Booking -- read a reservation before
charging (microservices-decisions.md §17). Payment mints the service token
here; Booking only verifies it (docs/auth/service-token-basics.md §4).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import jwt

from app.core.config import get_settings

# a slow Booking must not hang the guest's POST /payments
_TIMEOUT_SECONDS = 2.0
_TOKEN_TTL = timedelta(minutes=5)


class BookingUnavailableError(Exception):
    """Booking didn't answer in time, or answered with an error -- not "not found"."""


@dataclass(frozen=True)
class ReservationInfo:
    # the contract -- plain values, not Booking's ORM model. status is a
    # string ("pending"...), not Booking's ReservationStatus enum
    id: uuid.UUID
    guest_id: uuid.UUID
    status: str
    total_cents: int


def _service_token() -> str:
    # fresh per call: signing takes microseconds, so no cache to get wrong
    settings = get_settings()
    return jwt.encode(
        {"sub": "payment", "type": "service", "exp": datetime.now(UTC) + _TOKEN_TTL},
        settings.service_token_secret,
        algorithm=settings.jwt_algorithm,
    )


def _client() -> httpx.AsyncClient:
    # its own function so tests can point it at the app in-process (ASGITransport)
    return httpx.AsyncClient(base_url=get_settings().booking_base_url, timeout=_TIMEOUT_SECONDS)


async def get_reservation(reservation_id: uuid.UUID) -> ReservationInfo | None:
    """None if Booking has no such reservation; BookingUnavailableError if it
    couldn't be asked."""
    try:
        async with _client() as client:
            response = await client.get(
                f"/internal/reservations/{reservation_id}",
                headers={"Authorization": f"Bearer {_service_token()}"},
            )
    except httpx.HTTPError as exc:  # timeout, connection refused, ...
        raise BookingUnavailableError(str(exc)) from exc

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise BookingUnavailableError(f"Booking answered {response.status_code}")
    data = response.json()
    return ReservationInfo(
        id=uuid.UUID(data["id"]),
        guest_id=uuid.UUID(data["guest_id"]),
        status=data["status"],
        total_cents=data["total_cents"],
    )
