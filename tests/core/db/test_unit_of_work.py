import pytest

from app.core.db.unit_of_work import UnitOfWork


# stands in for AsyncSession -- __aexit__ only ever calls commit/rollback/close
# on it, so that's all this needs to fake (same minimal-fake approach as
# tests/reservation/test_cancel_reservation.py)
class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.closed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        self.closed = True


def _uow_with_fake_session() -> tuple[UnitOfWork, FakeSession]:
    # __new__, not UnitOfWork() -- skips __aenter__'s real DB session and
    # repositories, since __aexit__ doesn't touch either
    uow = UnitOfWork.__new__(UnitOfWork)
    session = FakeSession()
    uow.session = session
    return uow, session


# (BOOK-64 removed on_commit() and its four tests -- its only caller became a
# BookingCreated outbox event. What remains is the transaction itself.)


@pytest.mark.anyio
async def test_clean_exit_commits_and_closes() -> None:
    uow, session = _uow_with_fake_session()

    await uow.__aexit__(None, None, None)

    assert session.committed
    assert not session.rolled_back
    assert session.closed


@pytest.mark.anyio
async def test_exception_rolls_back_and_closes() -> None:
    uow, session = _uow_with_fake_session()

    await uow.__aexit__(ValueError, ValueError("boom"), None)

    assert session.rolled_back
    assert not session.committed
    assert session.closed
