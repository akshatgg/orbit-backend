"""Async engine + one transaction per request.

The Postgres functions ported from Orbit read the acting user from
`current_setting('app.current_user_id')` (see the `auth.uid()` shim in the
baseline migration). Every request therefore runs in a single transaction, and
`set_actor()` writes that setting with `is_local = true` so it disappears when
the request ends — pooled connections never carry one user's identity into the
next request.
"""

from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from .config import settings


def get_database_url() -> str:
    if not settings.DATABASE_URL:
        raise ValueError("DATABASE_URL is required. Set it in your .env file.")
    url = settings.DATABASE_URL
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


def get_sync_database_url() -> str:
    """Alembic runs migrations synchronously (psycopg2)."""
    if not settings.DATABASE_URL:
        raise ValueError("DATABASE_URL is required for migrations.")
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg2://", 1)
    return url


DATABASE_URL = get_database_url()

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800 if not settings.is_local else -1,
    pool_size=10 if not settings.is_local else 5,
    max_overflow=20 if not settings.is_local else 10,
    pool_timeout=30,
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """One session, one transaction. Commit on success, roll back on error."""
    async with AsyncSessionLocal() as session:
        try:
            # Mark this connection as the trusted writer for the ported guards
            # (see is_service() in alembic/sql/orbit_functions.sql).
            await session.execute(
                text("select set_config('app.trusted', 'on', true)")
            )
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def set_actor(db: AsyncSession, user_id: Optional[str]) -> None:
    """Make `auth.uid()` in the database answer with this user for this request."""
    await db.execute(
        text("select set_config('app.current_user_id', :uid, true)"),
        {"uid": user_id or ""},
    )
