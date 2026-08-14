from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.models import Base


class BaseRepository[ModelT: Base]:
    """Common CRUD every module's repository inherits instead of rewriting.

    Data access only, no business rules. Takes an existing session; never
    opens its own or calls commit()/rollback() -- that's the Unit of
    Work's job (BOOK-27). Methods return real model instances, never a
    SQLAlchemy Result/Row.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, id: UUID) -> ModelT | None:
        return await self.session.get(self.model, id)

    async def list(self, limit: int, offset: int) -> list[ModelT]:
        # order_by is required for LIMIT/OFFSET to paginate consistently
        # (docs/db/sql-basics.md#5). id is the only column every model is
        # guaranteed to have, so it's the only safe generic default here.
        stmt = select(self.model).order_by(self.model.id).limit(limit).offset(offset)
        result = await self.session.scalars(stmt)
        return list(result)

    def add(self, obj: ModelT) -> None:
        self.session.add(obj)

    async def delete(self, obj: ModelT) -> None:
        # a real, unconditional hard DELETE -- no soft-delete awareness
        # (see core/README.md's "repository.py" section). unlike add(),
        # delete() is async: cascades may need to load relationships
        # that aren't in memory yet, which is real I/O.
        await self.session.delete(obj)
