"""Seed the local dev database with realistic sample data.

Run with: uv run python -m app.core.seed
Safe to re-run: every seeded row has a fixed, hardcoded UUID (0000...0001-style),
so re-running looks each one up by primary key first and skips it if already
present, instead of inserting a duplicate.
"""

import asyncio
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User, UserRole
from app.core.models import Base
from app.core.unit_of_work import UnitOfWork
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from app.review.models import Review

# fixed UUIDs, not uuid4() -- re-running this script must generate the same
# ids every time so the get-or-create checks below actually find prior rows.
HOST_ALICE = uuid.UUID("00000000-0000-0000-0000-000000000001")
HOST_BOB = uuid.UUID("00000000-0000-0000-0000-000000000002")
GUEST_CAROL = uuid.UUID("00000000-0000-0000-0000-000000000003")
GUEST_DAVE = uuid.UUID("00000000-0000-0000-0000-000000000004")

PROPERTY_TLV_LOFT = uuid.UUID("00000000-0000-0000-0000-000000000101")
PROPERTY_HAIFA_COTTAGE = uuid.UUID("00000000-0000-0000-0000-000000000102")

RESERVATION_CAROL_TLV = uuid.UUID("00000000-0000-0000-0000-000000000201")
RESERVATION_DAVE_HAIFA = uuid.UUID("00000000-0000-0000-0000-000000000202")
RESERVATION_CAROL_HAIFA_CANCELLED = uuid.UUID("00000000-0000-0000-0000-000000000203")
RESERVATION_BOB_TLV = uuid.UUID("00000000-0000-0000-0000-000000000204")

PAYMENT_CAROL_TLV_CHARGE = uuid.UUID("00000000-0000-0000-0000-000000000301")
PAYMENT_DAVE_HAIFA_CHARGE = uuid.UUID("00000000-0000-0000-0000-000000000302")
PAYMENT_CAROL_HAIFA_CHARGE = uuid.UUID("00000000-0000-0000-0000-000000000303")
PAYMENT_CAROL_HAIFA_REFUND = uuid.UUID("00000000-0000-0000-0000-000000000304")

REVIEW_CAROL_ON_TLV_LOFT = uuid.UUID("00000000-0000-0000-0000-000000000401")
REVIEW_ALICE_ON_CAROL = uuid.UUID("00000000-0000-0000-0000-000000000402")
REVIEW_DAVE_ON_HAIFA_COTTAGE = uuid.UUID("00000000-0000-0000-0000-000000000403")
REVIEW_BOB_ON_TLV_LOFT = uuid.UUID("00000000-0000-0000-0000-000000000404")


async def get_or_create[ModelT: Base](
    session: AsyncSession, model: type[ModelT], id_: uuid.UUID, **fields: object
) -> ModelT:
    existing = await session.get(model, id_)
    if existing is not None:
        return existing
    obj = model(id=id_, **fields)
    session.add(obj)
    await session.flush()
    return obj


async def seed(uow: UnitOfWork) -> None:
    """2 hosts, 2 guests, 2 properties, 3 reservations (2 confirmed + 1
    cancelled -- makes EXCLUDE's WHERE status <> 'cancelled' visible in real
    data), a charge+refund payment pair for the cancelled one (a realistic
    lifecycle, not just a missing charge), and reviews in both directions:
    guest reviewing the property, host reviewing the guest.
    """
    session = uow.session
    host_alice = await get_or_create(
        session,
        User,
        HOST_ALICE,
        email="alice.host@example.com",
        display_name="Alice",
        hashed_password="seed-data-not-a-real-hash",
        role=UserRole.USER,
    )
    host_bob = await get_or_create(
        session,
        User,
        HOST_BOB,
        email="bob.host@example.com",
        display_name="Bob",
        hashed_password="seed-data-not-a-real-hash",
        role=UserRole.USER,
    )
    guest_carol = await get_or_create(
        session,
        User,
        GUEST_CAROL,
        email="carol.guest@example.com",
        display_name="Carol",
        hashed_password="seed-data-not-a-real-hash",
        role=UserRole.USER,
    )
    guest_dave = await get_or_create(
        session,
        User,
        GUEST_DAVE,
        email="dave.guest@example.com",
        display_name="Dave",
        hashed_password="seed-data-not-a-real-hash",
        role=UserRole.USER,
    )

    tlv_loft = await get_or_create(
        session,
        Property,
        PROPERTY_TLV_LOFT,
        host_id=host_alice.id,
        title="Sunny loft near the beach",
        description="A bright one-bedroom loft, five minutes from the beach.",
        city="Tel Aviv",
        country="IL",
        max_guests=3,
        price_per_night_cents=45000,
    )
    haifa_cottage = await get_or_create(
        session,
        Property,
        PROPERTY_HAIFA_COTTAGE,
        host_id=host_bob.id,
        title="Quiet cottage on Mount Carmel",
        description="A small cottage with a view over the bay.",
        city="Haifa",
        country="IL",
        max_guests=4,
        price_per_night_cents=32000,
    )

    res_carol_tlv = await get_or_create(
        session,
        Reservation,
        RESERVATION_CAROL_TLV,
        property_id=tlv_loft.id,
        guest_id=guest_carol.id,
        check_in=date(2026, 9, 1),
        check_out=date(2026, 9, 5),
        guest_count=2,
        total_cents=180000,
        status=ReservationStatus.CONFIRMED,
    )
    res_dave_haifa = await get_or_create(
        session,
        Reservation,
        RESERVATION_DAVE_HAIFA,
        property_id=haifa_cottage.id,
        guest_id=guest_dave.id,
        check_in=date(2026, 10, 1),
        check_out=date(2026, 10, 4),
        guest_count=3,
        total_cents=96000,
        status=ReservationStatus.CONFIRMED,
    )
    # cancelled on purpose: makes the EXCLUDE constraint's
    # WHERE status <> 'cancelled' behaviour visible in real data --
    # this reservation's dates are free for anyone else to book.
    res_carol_haifa_cancelled = await get_or_create(
        session,
        Reservation,
        RESERVATION_CAROL_HAIFA_CANCELLED,
        property_id=haifa_cottage.id,
        guest_id=guest_carol.id,
        check_in=date(2026, 11, 1),
        check_out=date(2026, 11, 3),
        guest_count=1,
        total_cents=64000,
        status=ReservationStatus.CANCELLED,
    )
    # the only COMPLETED stay in the seed set, and the only one whose dates are
    # in the past -- a review is only allowed on a completed reservation
    # (review/service.py), so without one the seed's reviews describe stays
    # that haven't happened yet. Bob hosts the Haifa cottage but books the
    # loft as a guest here; nothing stops a host from being someone else's
    # guest. Dates avoid Carol's Sep 1-5 booking so the EXCLUDE constraint on
    # overlapping reservations stays satisfied.
    res_bob_tlv = await get_or_create(
        session,
        Reservation,
        RESERVATION_BOB_TLV,
        property_id=tlv_loft.id,
        guest_id=host_bob.id,
        check_in=date(2026, 8, 10),
        check_out=date(2026, 8, 14),
        guest_count=2,
        total_cents=180000,
        status=ReservationStatus.COMPLETED,
    )

    await get_or_create(
        session,
        Payment,
        PAYMENT_CAROL_TLV_CHARGE,
        reservation_id=res_carol_tlv.id,
        kind=PaymentKind.CHARGE,
        amount_cents=180000,
        status=PaymentStatus.SUCCEEDED,
    )
    await get_or_create(
        session,
        Payment,
        PAYMENT_DAVE_HAIFA_CHARGE,
        reservation_id=res_dave_haifa.id,
        kind=PaymentKind.CHARGE,
        amount_cents=96000,
        status=PaymentStatus.SUCCEEDED,
    )
    # the cancelled reservation still shows a full charge+refund history,
    # not just a missing charge -- matches PaymentKind.REFUND's real use.
    await get_or_create(
        session,
        Payment,
        PAYMENT_CAROL_HAIFA_CHARGE,
        reservation_id=res_carol_haifa_cancelled.id,
        kind=PaymentKind.CHARGE,
        amount_cents=64000,
        status=PaymentStatus.SUCCEEDED,
    )
    await get_or_create(
        session,
        Payment,
        PAYMENT_CAROL_HAIFA_REFUND,
        reservation_id=res_carol_haifa_cancelled.id,
        kind=PaymentKind.REFUND,
        amount_cents=64000,
        status=PaymentStatus.SUCCEEDED,
    )

    # both review directions: guest reviewing the property, host reviewing the guest
    await get_or_create(
        session,
        Review,
        REVIEW_CAROL_ON_TLV_LOFT,
        reservation_id=res_carol_tlv.id,
        author_id=guest_carol.id,
        rating=5,
        comment="Lovely loft, exactly as described and very close to the beach.",
    )
    await get_or_create(
        session,
        Review,
        REVIEW_ALICE_ON_CAROL,
        reservation_id=res_carol_tlv.id,
        author_id=host_alice.id,
        rating=5,
        comment="Great guest, left the place spotless.",
    )
    # deliberately a lower rating than the loft's, and on the cheaper property:
    # it gives BOOK-77's "recommended" sort something to actually reorder --
    # price ASC puts the cottage first, recommended puts the loft first, so the
    # two sorts are distinguishable instead of coincidentally identical
    await get_or_create(
        session,
        Review,
        REVIEW_DAVE_ON_HAIFA_COTTAGE,
        reservation_id=res_dave_haifa.id,
        author_id=guest_dave.id,
        rating=3,
        comment="Nice view, but further from town than we expected.",
    )
    # a 4 alongside Carol's 5 on the same property: the loft's rating becomes a
    # mean of differing scores rather than one rating echoed back, which is
    # what makes it visible that the number is computed per request (BOOK-77)
    # and not stored on the listing
    await get_or_create(
        session,
        Review,
        REVIEW_BOB_ON_TLV_LOFT,
        reservation_id=res_bob_tlv.id,
        author_id=host_bob.id,
        rating=4,
        comment="Comfortable and well located. Shower pressure could be better.",
    )


async def main() -> None:
    async with UnitOfWork() as uow:
        await seed(uow)
    print("Seed data ready.")


if __name__ == "__main__":
    asyncio.run(main())
