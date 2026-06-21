"""异步 SQLAlchemy engine + sessionmaker。

提供：
- async_engine: 异步引擎（postgresql+asyncpg://，由 database_url 转换）
- AsyncSessionLocal: 异步会话工厂
- get_db: FastAPI 依赖注入，yield 异步会话

说明：
- Alembic（同步）使用 postgresql+psycopg://
- 应用层（异步）使用 postgresql+asyncpg://
- 配置项 DATABASE_URL 统一为 postgresql+psycopg://，此处按需转换
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_settings = get_settings()

# 将 postgresql+psycopg:// 转为 postgresql+asyncpg:// 以使用 async engine
# asyncpg 在高并发异步场景下性能优于 psycopg3 异步模式
_async_url = _settings.database_url.replace(
    "postgresql+psycopg://", "postgresql+asyncpg://"
)

async_engine = create_async_engine(
    _async_url,
    echo=False,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖注入：提供异步会话，请求结束自动关闭。

    异常时回滚并 re-raise，禁止吞没。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


if __name__ == "__main__":
    # 自测入口：验证 engine 创建（不连接数据库）
    print(f"async_engine.url={async_engine.url}")
    print(f"AsyncSessionLocal={AsyncSessionLocal}")
    print("OK")
