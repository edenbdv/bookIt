from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import Select, Subquery, func, select

from app.core.db.repository import BaseRepository
from app.property.models import Property, PropertyPhoto
from app.reservation.models import Reservation, ReservationStatus
from app.review.models import Review

PropertySort = Literal["price", "recommended"]


def _review_stats_subquery(property_ids: Select | None = None) -> Subquery:
    """Per-property rating sum, average and count, as a joinable subquery.

    Reaches property_id through Reservation because Review has no property_id
    column of its own, and counts guest->property reviews only -- a host's
    review OF a guest says nothing about the listing. Same direction filter as
    ReviewRepository.list_by_property_id.

    property_ids optionally restricts the aggregation to those ids instead
    of the whole table -- BOOK-92.
    """
    stmt = (
        select(
            Reservation.property_id.label("property_id"),
            func.sum(Review.rating).label("rating_sum"),
            func.avg(Review.rating).label("rating_avg"),
            func.count(Review.id).label("review_count"),
        )
        .select_from(Review)
        .join(Reservation, Review.reservation_id == Reservation.id)
        .where(Review.author_id == Reservation.guest_id)
    )
    if property_ids is not None:
        stmt = stmt.where(Reservation.property_id.in_(property_ids))
    return stmt.group_by(Reservation.property_id).subquery()


class PropertyRepository(BaseRepository[Property]):
    model = Property

    async def get_active_by_id(self, id: UUID) -> Property | None:
        # unlike the inherited get_by_id, excludes soft-deleted rows -- a
        # delisted property shouldn't be fetchable as if it were still live
        stmt = select(Property).where(Property.id == id, Property.deleted_at.is_(None))
        return await self.session.scalar(stmt)

    async def get_global_mean_rating(self) -> float:
        """C in the Bayesian formula: mean guest->property rating across all listings.

        Executed independently (not embedded in list_filtered's query) so
        the result can be cache-aside'd at the service layer -- BOOK-40.
        """
        stmt = (
            select(func.avg(Review.rating))
            .select_from(Review)
            .join(Reservation, Review.reservation_id == Reservation.id)
            .where(Review.author_id == Reservation.guest_id)
        )
        result = await self.session.scalar(stmt)
        return float(result) if result is not None else 0.0

    async def list_filtered(
        self,
        limit: int,
        offset: int,
        city: str | None = None,
        guests: int | None = None,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        check_in: date | None = None,
        check_out: date | None = None,
        # keyword-only and required, no defaults: router.py is the single place
        # the default sort is declared. A default here would be unreachable
        # (every caller passes one) and free to silently drift from the real
        # one. Keyword-only because a required arg can't follow defaulted ones.
        *,
        sort: PropertySort,
        # Bayesian smoothing constant (settings.rating_smoothing_m), passed in
        # rather than read here so the repository stays pure data access with
        # no config dependency -- service.py owns reading settings.
        smoothing_m: int,
        # C in the Bayesian formula, cache-aside'd by the caller (BOOK-40)
        global_mean_rating: float = 0.0,
    ) -> list[Property]:
        # every filter is optional -- omit one, it just doesn't narrow the
        # results. deleted_at IS NULL isn't optional: browsing/search should
        # never surface a delisted property (same reasoning as
        # get_active_by_id above).
        #
        # Filter Property first so the review aggregation below can be
        # scoped to the survivors instead of the whole table (BOOK-92).
        candidate_ids = select(Property.id).where(Property.deleted_at.is_(None))
        # only worth restricting stats to candidate_ids when it's a real
        # subset -- a net loss on an unfiltered browse (BOOK-92)
        has_filters = False
        if city is not None:
            # case-insensitive: a guest typing "tel aviv" should still match
            # a listing stored as "Tel Aviv". This can't use the plain
            # ix_properties_city btree index (it would need a matching
            # expression index on lower(city), or the citext extension) --
            # fine at today's data volume, worth revisiting if city search
            # ever shows up slow.
            candidate_ids = candidate_ids.where(func.lower(Property.city) == city.lower())
            has_filters = True
        if guests is not None:
            # guests = size of the group searching, NOT an upper bound --
            # >= on purpose: a property that fits more than `guests` still
            # works. Same relationship as the reservation-time rule
            # guest_count <= property.max_guests (docs/db/er-diagram.md)
            candidate_ids = candidate_ids.where(Property.max_guests >= guests)
            has_filters = True
        if min_price_cents is not None:
            candidate_ids = candidate_ids.where(Property.price_per_night_cents >= min_price_cents)
            has_filters = True
        if max_price_cents is not None:
            candidate_ids = candidate_ids.where(Property.price_per_night_cents <= max_price_cents)
            has_filters = True
        if check_in is not None and check_out is not None:
            # a property is available iff no non-cancelled reservation's date
            # range overlaps the requested one -- same '[)' range and the same
            # && (overlaps) test as the EXCLUDE constraint that enforces this
            # at the DB level (app/reservation/models.py:50-56), so "available
            # here" and "bookable without conflict" can never disagree.
            overlapping_reservation = (
                select(Reservation.id)
                .where(
                    Reservation.property_id == Property.id,
                    Reservation.status != ReservationStatus.CANCELLED,
                    func.daterange(Reservation.check_in, Reservation.check_out, "[)").op("&&")(
                        func.daterange(check_in, check_out, "[)")
                    ),
                )
                .exists()
            )
            candidate_ids = candidate_ids.where(~overlapping_reservation)
            has_filters = True
        candidate_ids = candidate_ids.subquery()

        # LEFT (outer) join, not inner: a property with no reviews must still
        # appear in the listing, with NULL stats. The aggregate rides along on
        # the main query rather than being fetched separately like cover_photo
        # -- sorting by it requires it in this statement.
        stats = _review_stats_subquery(
            property_ids=select(candidate_ids.c.id) if has_filters else None
        )
        stmt = (
            select(Property, stats.c.rating_avg, stats.c.review_count)
            .join(candidate_ids, Property.id == candidate_ids.c.id)
            .outerjoin(stats, Property.id == stats.c.property_id)
        )

        # order_by is required for LIMIT/OFFSET to paginate consistently
        # (docs/db/sql-basics.md#5); id is the final tiebreaker in both sorts
        # since neither price nor score is unique.
        #
        # "recommended" is the default as of BOOK-77, replacing the price ASC
        # placeholder BOOK-29 shipped with. Price ASC was never the intended
        # long-term default -- it was what existed before Review rows did.
        # Price remains available as an explicit choice.
        if sort == "recommended":
            # Bayesian-smoothed average rating: pulls a property's score
            # toward the global mean when it has few reviews, so one 5-star
            # review doesn't outrank hundreds of real reviews averaging 4.8.
            #
            # coalesce: no reviews -> score collapses to C, ranking an
            # unrated property at the global mean (mid-table) rather than
            # last -- it's unknown, not bad, and burying it means it can
            # never get a first review.
            global_mean = global_mean_rating  # C -- already a plain float, NULL-guarded in get_global_mean_rating
            rating_sum = func.coalesce(stats.c.rating_sum, 0)  # v*R
            review_count = func.coalesce(stats.c.review_count, 0)  # v
            score = (rating_sum + smoothing_m * global_mean) / (review_count + smoothing_m)
            stmt = stmt.order_by(score.desc(), Property.price_per_night_cents.asc())
        else:
            stmt = stmt.order_by(Property.price_per_night_cents.asc())
        stmt = stmt.order_by(Property.id.asc()).limit(limit).offset(offset)

        # execute, not scalars: the rows carry the joined aggregate columns
        # alongside the Property entity
        result = await self.session.execute(stmt)
        properties = []
        for property_, rating_avg, review_count in result:
            # not mapped columns -- attached so PropertyListRead's
            # from_attributes=True can read them as flat fields (same
            # technique as cover_photo); never persisted
            property_.rating_avg = round(float(rating_avg), 2) if rating_avg is not None else None
            property_.review_count = review_count or 0
            properties.append(property_)
        return properties

    async def get_review_stats(self, property_id: UUID) -> tuple[float | None, int]:
        """(average rating, review count) for one property, for the detail page."""
        stats = _review_stats_subquery()
        stmt = select(stats.c.rating_avg, stats.c.review_count).where(
            stats.c.property_id == property_id
        )
        row = (await self.session.execute(stmt)).first()
        if row is None or row.rating_avg is None:
            return None, 0
        return round(float(row.rating_avg), 2), row.review_count

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
