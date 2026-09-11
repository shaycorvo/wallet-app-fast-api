from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings

settings = get_settings()
database_url = settings.database_url
if database_url.startswith("postgres://"):
    database_url = "postgresql+asyncpg://" + database_url.removeprefix("postgres://")
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+asyncpg://" + database_url.removeprefix("postgresql://")

engine = create_async_engine(
    database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=2,
    pool_pre_ping=True,
    connect_args={"server_settings": {"statement_timeout": "5000", "lock_timeout": "2000"}},
)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session


async def close_database() -> None:
    await engine.dispose()