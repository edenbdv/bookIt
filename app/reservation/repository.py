from app.core.repository import BaseRepository
from app.reservation.models import Reservation


class ReservationRepository(BaseRepository[Reservation]):
    model = Reservation
