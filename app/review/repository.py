from app.core.repository import BaseRepository
from app.review.models import Review


class ReviewRepository(BaseRepository[Review]):
    model = Review
