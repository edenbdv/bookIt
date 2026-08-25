from uuid import UUID

from sqlalchemy import func, select

from app.core.repository import BaseRepository
from app.property.models import Property, PropertyPhoto


class PropertyRepository(BaseRepository[Property]):
    model = Property

    async def get_active_by_id(self, id: UUID) -> Property | None:
        # unlike the inherited get_by_id, excludes soft-deleted rows -- a
        # delisted property shouldn't be fetchable as if it were still live
        stmt = select(Property).where(Property.id == id, Property.deleted_at.is_(None))
        return await self.session.scalar(stmt)

    async def list_filtered(
        self,
        limit: int,
        offset: int,
        city: str | None = None,
        guests: int | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
    ) -> list[Property]:
        # every filter is optional -- omit one, it just doesn't narrow the
        # results. deleted_at IS NULL isn't optional: browsing/search should
        # never surface a delisted property (same reasoning as
        # get_active_by_id above).
        stmt = select(Property).where(Property.deleted_at.is_(None))
        if city is not None:
            # case-insensitive: a guest typing "tel aviv" should still match
            # a listing stored as "Tel Aviv". This can't use the plain
            # ix_properties_city btree index (it would need a matching
            # expression index on lower(city), or the citext extension) --
            # fine at today's data volume, worth revisiting if city search
            # ever shows up slow.
            stmt = stmt.where(func.lower(Property.city) == city.lower())
        if guests is not None:
            # guests = size of the group searching, NOT an upper bound --
            # >= on purpose: a property that fits more than `guests` still
            # works. Same relationship as the reservation-time rule
            # guest_count <= property.max_guests (docs/db/er-diagram.md)
            stmt = stmt.where(Property.max_guests >= guests)
        if min_price_cents is not None:
            stmt = stmt.where(Property.price_per_night_cents >= min_price_cents)
        if max_price_cents is not None:
            stmt = stmt.where(Property.price_per_night_cents <= max_price_cents)

        # order_by is required for LIMIT/OFFSET to paginate consistently
        # (docs/db/sql-basics.md#5); id is a tiebreaker since price alone
        # isn't unique. price ASC is a placeholder default, not the intended
        # long-term one -- Airbnb/Booking.com default to a "Recommended"
        # sort (avg. rating), which needs BOOK-73's Review rows aggregated
        # first. See the BOOK-29/BOOK-73 comment thread on this.
        stmt = stmt.order_by(Property.price_per_night_cents.asc(), Property.id.asc())
        stmt = stmt.limit(limit).offset(offset)

        result = await self.session.scalars(stmt)
        return list(result)

    async def list_distinct_cities(self) -> list[str]:
        # powers the search bar's autocomplete -- excludes delisted
        # properties so a city never gets suggested if searching it would
        # always come back empty
        stmt = (
            select(Property.city)
            .where(Property.deleted_at.is_(None))
            .distinct()
            .order_by(Property.city.asc())
        )
        result = await self.session.scalars(stmt)
        return list(result)


class PropertyPhotoRepository(BaseRepository[PropertyPhoto]):
    model = PropertyPhoto

    async def list_by_property_id(self, property_id: UUID) -> list[PropertyPhoto]:
        # no limit/offset -- a property's photo count is naturally small
        # and bounded, unlike a browse/search listing; AC doesn't ask for
        # pagination here
        stmt = (
            select(PropertyPhoto)
            .where(PropertyPhoto.property_id == property_id)
            .order_by(PropertyPhoto.position.asc())
        )
        result = await self.session.scalars(stmt)
        return list(result)

    async def list_cover_photos(self, property_ids: list[UUID]) -> list[PropertyPhoto]:
        # DISTINCT ON (Postgres-only, docs/db/sql-basics.md#7): one row per
        # property_id, the lowest-position one -- a list page's cover photo,
        # without loading every photo of every property on the page
        stmt = (
            select(PropertyPhoto)
            .distinct(PropertyPhoto.property_id)
            .where(PropertyPhoto.property_id.in_(property_ids))
            .order_by(PropertyPhoto.property_id, PropertyPhoto.position.asc())
        )
        result = await self.session.scalars(stmt)
        return list(result)
