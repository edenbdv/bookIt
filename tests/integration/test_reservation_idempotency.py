"""BOOK-61: Idempotency-Key handling on POST /reservations (BOOK-36) against
a real Postgres -- ported from scripts/verify_idempotency_keys.py.

Through the API rather than the service directly (the script called
create_reservation_idempotent itself), so each domain error's HTTP mapping
in reservation/router.py is checked too, not just that it's raised.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.property.models import Property
from app.reservation.models import Reservation
from tests.integration.helpers import auth_headers, booking, make_property, make_user

CHECK_IN = datetime.now(UTC).date() + timedelta(days=30)


@pytest.fixture
async def listing(db_session: AsyncSession) -> tuple[Property, User, User]:
    host = await make_user(db_session, "host@test.com")
    guest_a = await make_user(db_session, "guest-a@test.com")
    guest_b = await make_user(db_session, "guest-b@test.com")
    prop = await make_property(db_session, host, max_guests=4)
    await db_session.commit()
    return prop, guest_a, guest_b


async def _count_reservations(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Reservation))


@pytest.mark.anyio
async def test_retry_with_same_key_returns_the_same_reservation(
    api_client: AsyncClient, db_session: AsyncSession, listing: tuple[Property, User, User]
) -> None:
    prop, guest_a, _ = listing
    body = booking(prop, CHECK_IN, nights=4)

    first = await api_client.post("/reservations", json=body, headers=auth_headers(guest_a, "K1"))
    retry = await api_client.post("/reservations", json=body, headers=auth_headers(guest_a, "K1"))

    assert first.status_code == retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"]
    assert await _count_reservations(db_session) == 1


# same key, different body -- rejected explicitly, not silently answered
# with the first request's result, and not silently treated as a new one
@pytest.mark.anyio
async def test_same_key_with_a_different_body_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession, listing: tuple[Property, User, User]
) -> None:
    prop, guest_a, _ = listing
    await api_client.post(
        "/reservations", json=booking(prop, CHECK_IN, nights=4), headers=auth_headers(guest_a, "K1")
    )

    mismatched = await api_client.post(
        "/reservations",
        json=booking(prop, CHECK_IN, nights=4, guest_count=1),
        headers=auth_headers(guest_a, "K1"),
    )

    assert mismatched.status_code == 400
    assert await _count_reservations(db_session) == 1


# keys aren't scoped per guest -- another guest reusing (or guessing) a key
# must not get the first guest's reservation back
@pytest.mark.anyio
async def test_another_guest_reusing_a_key_does_not_get_the_reservation(
    api_client: AsyncClient, db_session: AsyncSession, listing: tuple[Property, User, User]
) -> None:
    prop, guest_a, guest_b = listing
    body = booking(prop, CHECK_IN, nights=4)
    first = await api_client.post("/reservations", json=body, headers=auth_headers(guest_a, "K1"))

    reused = await api_client.post("/reservations", json=body, headers=auth_headers(guest_b, "K1"))

    assert reused.status_code == 409
    assert first.json()["id"] not in reused.text
    assert await _count_reservations(db_session) == 1


# a new key is a new request -- it must not bypass the EXCLUDE constraint
@pytest.mark.anyio
async def test_new_key_for_the_same_dates_still_conflicts(
    api_client: AsyncClient, db_session: AsyncSession, listing: tuple[Property, User, User]
) -> None:
    prop, guest_a, _ = listing
    body = booking(prop, CHECK_IN, nights=4)
    await api_client.post("/reservations", json=body, headers=auth_headers(guest_a, "K1"))

    second = await api_client.post("/reservations", json=body, headers=auth_headers(guest_a, "K2"))

    assert second.status_code == 409
    assert second.json()["detail"] == "These dates are no longer available for this property"
    assert await _count_reservations(db_session) == 1


# a request that fails validation rolls back entirely, PROCESSING key row
# included -- so the key isn't permanently burned
@pytest.mark.anyio
async def test_failed_attempt_does_not_burn_the_key(
    api_client: AsyncClient, db_session: AsyncSession, listing: tuple[Property, User, User]
) -> None:
    prop, guest_a, _ = listing

    too_many_guests = await api_client.post(
        "/reservations",
        json=booking(prop, CHECK_IN, nights=4, guest_count=99),
        headers=auth_headers(guest_a, "K3"),
    )
    assert too_many_guests.status_code == 400

    retry = await api_client.post(
        "/reservations", json=booking(prop, CHECK_IN, nights=4), headers=auth_headers(guest_a, "K3")
    )
    assert retry.status_code == 201
    assert await _count_reservations(db_session) == 1


# two genuinely simultaneous requests with the same brand-new key -- each
# goes through get_uow on its own session/connection, so the PK race on
# idempotency_keys is real. Whichever wins, exactly one reservation exists.
@pytest.mark.anyio
async def test_concurrent_requests_with_the_same_key_create_one_reservation(
    api_client: AsyncClient, db_session: AsyncSession, listing: tuple[Property, User, User]
) -> None:
    prop, guest_a, _ = listing
    body = booking(prop, CHECK_IN, nights=4)

    responses = await asyncio.gather(
        *(
            api_client.post("/reservations", json=body, headers=auth_headers(guest_a, "K4"))
            for _ in range(2)
        )
    )

    statuses = sorted(r.status_code for r in responses)
    # 201+201 (loser saw the winner's COMPLETED key) or 201+409 (loser saw it
    # still PROCESSING) are both correct -- a 500 never is
    assert statuses in ([201, 201], [201, 409])
    assert await _count_reservations(db_session) == 1
