"""监控 Universe 构建服务（M1）。

聚合所有用户的自选股，去重生成监控股票池。

核心函数：
- build_monitoring_universe(): 聚合所有用户 active 自选，去重生成监控股票池
- get_universe_for_user(user_id): 获取某用户的自选股集合

设计说明：
- 向量化去重：使用 SQL SELECT DISTINCT 在数据库层去重，避免 Python 层 for 循环
- 仅聚合 active=true 的自选记录（软删除记录不参与）
- 返回 instrument_id 集合（Set[UUID]），供监控调度器消费
- 加入自选即参与当前启用的监控方案：universe 是所有用户自选的并集
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.watchlist import UserWatchlistItem


async def build_monitoring_universe(db: AsyncSession) -> set[UUID]:
    """聚合所有用户自选，去重生成监控股票池。

    使用 SQL DISTINCT 在数据库层去重（向量化），避免 Python 层 for 循环。
    仅聚合 active=true 的记录。

    Args:
        db: 异步数据库会话

    Returns:
        去重后的 instrument_id 集合（Set[UUID]）
    """
    # SQL DISTINCT 在数据库层去重，单次查询返回所有活跃自选的 instrument_id
    stmt = (
        select(UserWatchlistItem.instrument_id)
        .where(UserWatchlistItem.active.is_(True))
        .distinct()
    )
    result = await db.execute(stmt)
    # 一次性构建 set，无 for 循环
    return {row[0] for row in result.all()}


async def get_universe_for_user(db: AsyncSession, user_id: UUID) -> set[UUID]:
    """获取某用户的自选股集合。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        该用户 active 自选的 instrument_id 集合
    """
    stmt = select(UserWatchlistItem.instrument_id).where(
        UserWatchlistItem.user_id == user_id,
        UserWatchlistItem.active.is_(True),
    )
    result = await db.execute(stmt)
    return {row[0] for row in result.all()}


async def get_universe_count(db: AsyncSession) -> int:
    """获取监控 universe 的去重股票数量（用于监控调度器快速判断是否有任务）。

    Args:
        db: 异步数据库会话

    Returns:
        去重后的股票数量
    """
    stmt = (
        select(UserWatchlistItem.instrument_id)
        .where(UserWatchlistItem.active.is_(True))
        .distinct()
    )
    result = await db.execute(stmt)
    return len(result.all())


if __name__ == "__main__":
    # 自测入口：验证函数定义与可调用性（无副作用，不连接数据库）
    print(f"build_monitoring_universe={build_monitoring_universe}")
    print(f"get_universe_for_user={get_universe_for_user}")
    print(f"get_universe_count={get_universe_count}")
    # 验证返回类型注解
    import inspect
    sig_build = inspect.signature(build_monitoring_universe)
    sig_user = inspect.signature(get_universe_for_user)
    print(f"build_monitoring_universe params={list(sig_build.parameters.keys())}")
    print(f"get_universe_for_user params={list(sig_user.parameters.keys())}")
    assert "db" in sig_build.parameters
    assert "user_id" in sig_user.parameters
    print("OK")
