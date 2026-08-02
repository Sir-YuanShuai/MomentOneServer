from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise ValueError("MOMENT_ONE_DATABASE_URL is required to initialize the database")

        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


# 全局单例，在 application lifespan 中初始化
_db: Database | None = None


def init_database(settings: Settings) -> Database:
    global _db
    _db = Database(settings)
    return _db


def get_database() -> Database:
    if _db is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _db


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：提供一个数据库 session，请求结束自动关闭。"""
    db = get_database()
    async with db.session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
