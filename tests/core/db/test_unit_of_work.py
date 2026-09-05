import pytest

from app.core.db.unit_of_work import UnitOfWork


# stands in for AsyncSession -- __aexit__ only ever calls commit/rollback/close
# on it, so that's all this needs to fake (same minimal-fake approach as
# tests/reservation/test_cancel_reservation.py)
class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def close(self) -> None:
        pass


def _uow_with_fake_session() -> tuple[UnitOfWork, FakeSession]:
    # __new__, not UnitOfWork() -- skips __aenter__'s real DB session and
    # repositories, since on_commit()/__aexit__ don't touch either
    uow = UnitOfWork.__new__(UnitOfWork)
    session = FakeSession()
    uow.session = session
    uow._on_commit = []
    return uow, session


# BOOK-45: this is the mechanism worker-decisions.md section 9 relies on to
# close the enqueue-before-commit gap -- a callback registered here must
# never fire for a reservation that didn't actually get persisted.
@pytest.mark.anyio
async def test_on_commit_callback_fires_after_a_clean_exit() -> None:
    uow, session = _uow_with_fake_session()
    fired = []
    uow.on_commit(lambda: fired.append("called"))

    await uow.__aexit__(None, None, None)

    assert session.committed
    assert fired == ["called"]


@pytest.mark.anyio
async def test_on_commit_callback_does_not_fire_on_rollback() -> None:
    uow, session = _uow_with_fake_session()
    fired = []
    uow.on_commit(lambda: fired.append("called"))

    await uow.__aexit__(ValueError, ValueError("boom"), None)

    assert session.rolled_back
    assert fired == []
