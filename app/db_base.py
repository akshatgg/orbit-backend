"""Single SQLAlchemy DeclarativeBase shared by every domain model."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
