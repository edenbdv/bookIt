from sqlalchemy import select

from app.auth.models import User
from app.core.db.repository import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        # email is unique+indexed (models.py), so at most one row can match
        stmt = select(User).where(User.email == email)
        return await self.session.scalar(stmt)
