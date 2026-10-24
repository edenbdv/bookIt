"""BOOK-66: Booking's /internal routes -- for other services, never guests.
Service-token auth (docs/auth/service-token-basics.md); the gateway (BOOK-67)
blocks the whole /internal prefix from outside.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.dependencies import require_service
from app.core.db.unit_of_work import UnitOfWork, get_uow
from app.reservation.models import Reservation
from app.reservation.schemas import ReservationInternalRead

router = APIRouter(prefix="/internal", tags=["internal"])


# the one sync call between the services: Payment reads the reservation before
# charging -- it decides on current state while the guest waits
# (microservices-decisions.md §17)
@router.get("/reservations/{reservation_id}", response_model=ReservationInternalRead)
async def get_reservation_for_service(
    reservation_id: uuid.UUID,
    _caller: str = Depends(require_service("payment")),
    uow: UnitOfWork = Depends(get_uow),
) -> Reservation:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found")
    return reservation
