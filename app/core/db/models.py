from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Every model inherits from this.

    Defining a subclass registers its table in Base.metadata — the single
    catalog SQLAlchemy uses to resolve relationships between models, and
    that Alembic diffs against the live database to generate migrations.

    A model only reaches the catalog if its module is imported, so Alembic
    needs somewhere that imports all of them (see BOOK-23).
    """
