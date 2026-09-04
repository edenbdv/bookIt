from app.core.db.repository import BaseRepository
from app.reservation.models import Reservation


class ReservationRepository(BaseRepository[Reservation]):
    model = Reservation
