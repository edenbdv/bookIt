import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


# permission tier only — hosting/booking are actions, not roles
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    # client-side default: id is available immediately, no DB round-trip needed
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    hashed_password: Mapped[str]
    role: Mapped[UserRole] = mapped_column(default=UserRole.USER)
    # server-side default: DB's clock is the source of truth, not the app's
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
