"""BOOK-61: resolving a PENDING payment through the real webhook (BOOK-55)
against a real Postgres -- ported from scripts/verify_webhook_resolution.py.

Each test first leaves a payment PENDING the way production would: a crash
during the provider call, after the row is already committed (see
test_payment_saga.py). Then, unlike the script (which called
resolve_payment_and_react directly), the provider's callback goes through
POST /payments/webhook with a real HMAC signature -- so the signature check
and the router are in the loop too.
"""

import json
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.core.db.unit_of_work import UnitOfWork
from app.payment.models import PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.saga import create_payment_and_react
from app.payment.schemas import PaymentCreate
from app.payment.webhook_auth import sign_webhook_payload
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import make_pending_reservation, payment_rows, reservation_status


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
            await create_payment_and_react(
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


async def _payment_status(db_session: AsyncSession, reservation: Reservation, kind: PaymentKind):
    [status] = [s for _, k, s in await payment_rows(db_session, reservation) if k == kind]
    return status


@pytest.mark.anyio
async def test_webhook_success_resolves_the_charge_and_confirms(
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
    assert await reservation_status(db_session, reservation) == ReservationStatus.CONFIRMED


# providers retry deliveries -- a repeat, even with a contradicting outcome,
# must be a no-op, not a second reaction
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
    assert await reservation_status(db_session, reservation) == ReservationStatus.CONFIRMED


@pytest.mark.anyio
async def test_webhook_failure_cancels_the_reservation(
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
    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED


# a REFUND left PENDING (crash mid-refund, during BOOK-54's compensation)
# resolves the same way -- with no further reaction, since the hold was
# already released when the refund was issued
@pytest.mark.anyio
async def test_webhook_resolves_a_pending_refund(
    api_client: AsyncClient,
    db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation
    with (
        patch(
            "app.payment.saga.reservation_service.confirm_reservation",
            side_effect=RuntimeError("simulated confirm failure"),
        ),
        patch("app.payment.service.provider_refund", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError),
    ):
        async with UnitOfWork() as uow:
            await create_payment_and_react(
                uow,
                PaymentCreate(reservation_id=reservation.id),
                guest.id,
                str(uuid.uuid4()),
                force=PaymentOutcome.SUCCESS,
            )
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
