from app.auth.models import User
from app.core.repository import BaseRepository


class UserRepository(BaseRepository[User]):
    model = User
