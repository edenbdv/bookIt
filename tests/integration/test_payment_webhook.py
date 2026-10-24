"""BOOK-61: resolving a PENDING payment through the real webhook (BOOK-55)
against a real Postgres -- ported from scripts/verify_webhook_resolution.py.

Each test first leaves a payment PENDING the way production would: a crash
during the provider call, after the row is already committed (see
test_payment_saga.py). Then, unlike the script (which called
resolve_payment directly), the provider's callback goes through
POST /payments/webhook with a real HMAC signature -- so the signature check
and the router are in the loop too.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.core.db.unit_of_work import UnitOfWork
from app.payment.models import PaymentKind, PaymentOutboxEvent, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.schemas import PaymentCreate
from app.payment.service import create_refund, pay_reservation
from app.payment.webhook_auth import sign_webhook_payload
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import make_pending_reservation, payment_rows, reservation_status

# BOOK-66: paying asks Booking over HTTP -- reach it in-process
pytestmark = pytest.mark.usefixtures("booking_in_process")


@pytest.fixture
async def pending_reservation(db_session: AsyncSession) -> tuple[Reservation, User]:
    return await make_pending_reservation(db_session)


async def _charge_left_pending(
    db_session: AsyncSession, reservation: Reservation, guest: User
) -> uuid.UUID:
    # crash mid-call to the provider -- the row is already committed PENDING
    with (
        patch("app.payment.service.charge", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError),
    ):
        async with UnitOfWork() as uow:
            await pay_reservation(
                uow, PaymentCreate(reservation_id=reservation.id), guest.id, str(uuid.uuid4())
            )
    [(payment_id, _, _)] = await payment_rows(db_session, reservation)
    return payment_id


async def _send_webhook(
    client: AsyncClient,
    payment_id: uuid.UUID,
    outcome: PaymentOutcome,
    signature: str | None = None,
) -> Response:
    # signed over the exact bytes sent -- the router verifies the raw body,
    # not a reparsed model (payment/router.py)
    body = json.dumps({"payment_id": str(payment_id), "outcome": outcome.value}).encode()
    return await client.post(
        "/payments/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature": signature or sign_webhook_payload(body),
        },
    )


# BOOK-65: the webhook no longer confirms/cancels itself -- it writes the
# payment-events row Booking confirms/cancels from (tested on Booking's side)
async def _events(db_session: AsyncSession, reservation: Reservation) -> list[str]:
    result = await db_session.scalars(
        select(PaymentOutboxEvent.event_type).where(
            PaymentOutboxEvent.reservation_id == reservation.id
        )
    )
    return [e.value for e in result]


async def _payment_status(db_session: AsyncSession, reservation: Reservation, kind: PaymentKind):
    [status] = [s for _, k, s in await payment_rows(db_session, reservation) if k == kind]
    return status


@pytest.mark.anyio
async def test_webhook_success_resolves_the_charge_and_writes_its_event(
    api_client: AsyncClient,
    db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation
    payment_id = await _charge_left_pending(db_session, reservation, guest)

    response = await _send_webhook(api_client, payment_id, PaymentOutcome.SUCCESS)

    assert response.status_code == 200
    assert await _payment_status(db_session, reservation, PaymentKind.CHARGE) == (
        PaymentStatus.SUCCEEDED
    )
    assert await _events(db_session, reservation) == ["payment_succeeded"]


# providers retry deliveries -- a repeat, even with a contradicting outcome,
# must be a no-op, not a second event
@pytest.mark.anyio
async def test_redelivered_webhook_is_a_no_op(
    api_client: AsyncClient,
    db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation
    payment_id = await _charge_left_pending(db_session, reservation, guest)
    await _send_webhook(api_client, payment_id, PaymentOutcome.SUCCESS)

    redelivery = await _send_webhook(api_client, payment_id, PaymentOutcome.FAILURE)

    assert redelivery.status_code == 200
    assert await _payment_status(db_session, reservation, PaymentKind.CHARGE) == (
        PaymentStatus.SUCCEEDED
    )
    assert await _events(db_session, reservation) == ["payment_succeeded"]


@pytest.mark.anyio
async def test_webhook_failure_resolves_the_charge_and_writes_its_event(
    api_client: AsyncClient,
    db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation
    payment_id = await _charge_left_pending(db_session, reservation, guest)

    response = await _send_webhook(api_client, payment_id, PaymentOutcome.FAILURE)

    assert response.status_code == 200
    assert await _payment_status(db_session, reservation, PaymentKind.CHARGE) == (
        PaymentStatus.FAILED
    )
    assert await _events(db_session, reservation) == ["payment_failed"]


# a REFUND left PENDING (crash mid-call to the provider) resolves the same
# way -- with no event, since a refund's outcome drives no reservation
@pytest.mark.anyio
async def test_webhook_resolves_a_pending_refund(
    api_client: AsyncClient,
    db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, _ = pending_reservation
    with (
        patch("app.payment.service.provider_refund", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError),
    ):
        async with UnitOfWork() as uow:
            await create_refund(uow, reservation.id, reservation.total_cents)
    [refund_id] = [
        pid
        for pid, kind, _ in await payment_rows(db_session, reservation)
        if kind == PaymentKind.REFUND
    ]

    response = await _send_webhook(api_client, refund_id, PaymentOutcome.SUCCESS)

    assert response.status_code == 200
    assert await _payment_status(db_session, reservation, PaymentKind.REFUND) == (
        PaymentStatus.SUCCEEDED
    )
    assert await _events(db_session, reservation) == []


# anyone who finds the URL can POST to it -- the signature is the only thing
# proving the caller is the provider (webhook-basics.md §8)
@pytest.mark.anyio
async def test_webhook_with_a_bad_signature_is_rejected(
    api_client: AsyncClient,
    db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation
    payment_id = await _charge_left_pending(db_session, reservation, guest)

    response = await _send_webhook(
        api_client, payment_id, PaymentOutcome.SUCCESS, signature="not-a-real-signature"
    )

    assert response.status_code == 401
    assert await _payment_status(db_session, reservation, PaymentKind.CHARGE) == (
        PaymentStatus.PENDING
    )
    assert await reservation_status(db_session, reservation) == ReservationStatus.PENDING
