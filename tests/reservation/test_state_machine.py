import pytest

from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import IllegalTransitionError, transition_to

LEGAL_TRANSITIONS = [
    (ReservationStatus.PENDING, ReservationStatus.CONFIRMED),
    (ReservationStatus.PENDING, ReservationStatus.CANCELLED),
    (ReservationStatus.CONFIRMED, ReservationStatus.CANCELLED),
    (ReservationStatus.CONFIRMED, ReservationStatus.COMPLETED),
]

# every (from, to) pair NOT in LEGAL_TRANSITIONS, including same-status
# "transitions" and anything out of a terminal state
ILLEGAL_TRANSITIONS = [
    (current, new)
    for current in ReservationStatus
    for new in ReservationStatus
    if (current, new) not in LEGAL_TRANSITIONS
]


@pytest.mark.parametrize("current_status,new_status", LEGAL_TRANSITIONS)
def test_legal_transition_updates_status(
    current_status: ReservationStatus, new_status: ReservationStatus
) -> None:
    reservation = Reservation(status=current_status)

    transition_to(reservation, new_status)

    assert reservation.status == new_status


@pytest.mark.parametrize("current_status,new_status", ILLEGAL_TRANSITIONS)
def test_illegal_transition_raises_and_leaves_status_unchanged(
    current_status: ReservationStatus, new_status: ReservationStatus
) -> None:
    reservation = Reservation(status=current_status)

    with pytest.raises(IllegalTransitionError):
        transition_to(reservation, new_status)

    assert reservation.status == current_status


@pytest.mark.parametrize(
    "terminal_status", [ReservationStatus.CANCELLED, ReservationStatus.COMPLETED]
)
def test_terminal_states_have_no_legal_exit(terminal_status: ReservationStatus) -> None:
    reservation = Reservation(status=terminal_status)

    for candidate in ReservationStatus:
        with pytest.raises(IllegalTransitionError):
            transition_to(reservation, candidate)
