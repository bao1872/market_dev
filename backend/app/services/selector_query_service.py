"""选股查询统一服务。

普通用户查询选股结果的唯一入口。校验 published run，执行服务端筛选/排序/分页，
返回 source_total、universe_total 和 filtered_total 三级计数。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import DSA_SELECTOR
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyRun
from app.models.watchlist import UserWatchlistItem
from app.repositories.strategy_result_repository import (
    MetricFilter,
    QueryResultPage,
    SortSpec,
    count_by_run,
    count_by_run_with_watchlist,
    query_results,
)


class SelectorQueryError(Exception):
    """选股查询错误基类。"""
    pass


class RunNotFoundError(SelectorQueryError):
    """Run 不存在或未发布。"""
    pass


class NotSelectorRunError(SelectorQueryError):
    """Run 不是选股策略。"""
    pass


class InvalidFilterError(SelectorQueryError):
    """筛选条件无效。"""
    pass


@dataclass
class SelectorResultPage:
    """选股查询结果分页。"""
    run_id: uuid.UUID
    strategy_key: str
    trade_date: date | None
    source_total: int  # 过滤前总数（全市场）
    universe_total: int  # 股票池内总数（watchlist 时为自选股范围，all 时等于 source_total）
    filtered_total: int  # 过滤后总数
    page: int
    page_size: int
    items: list[Any] = field(default_factory=list)


async def query_published_selector_results(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    filters: list[MetricFilter] | None = None,
    sort: SortSpec | None = None,
    page: int = 1,
    page_size: int = 50,
    universe: str = "all",
    keyword: str | None = None,
) -> SelectorResultPage:
    """查询已发布的选股策略结果。

    Args:
        db: 异步数据库会话
        run_id: StrategyRun ID（必须已发布）
        user_id: 当前用户 ID（用于 universe=watchlist）
        filters: 指标筛选条件
        sort: 排序规格
        page: 页码（从 1 开始）
        page_size: 每页条数
        universe: "all" 全市场 | "watchlist" 仅自选股

    Returns:
        SelectorResultPage 包含 source_total 和 filtered_total

    Raises:
        RunNotFoundError: run 不存在或未发布
        NotSelectorRunError: run 不是选股策略
        InvalidFilterError: 筛选条件无效
    """
    # 1. 校验 run 存在且已发布
    run = await db.get(StrategyRun, run_id)
    if run is None:
        raise RunNotFoundError(f"StrategyRun {run_id} 不存在")
    if run.published_at is None:
        raise RunNotFoundError(f"StrategyRun {run_id} 未发布")

    # 2. 校验策略类型为 selector
    version = await db.get(StrategyVersion, run.strategy_version_id)
    if version is None:
        raise RunNotFoundError(f"StrategyVersion {run.strategy_version_id} 不存在")
    definition = await db.get(StrategyDefinition, version.strategy_definition_id)
    if definition is None:
        raise RunNotFoundError(f"StrategyDefinition {version.strategy_definition_id} 不存在")
    if definition.kind != "selector":
        raise NotSelectorRunError(
            f"策略 {definition.strategy_key} 类型为 {definition.kind}，不是 selector"
        )

    # 3. 获取 source_total（过滤前总数，全市场）
    source_total = await count_by_run(db, run_id)

    # 4. 构建 universe 过滤（watchlist 时只返回用户自选股）
    watchlist_instrument_ids: set[uuid.UUID] | None = None
    universe_total = source_total  # 默认 universe=all，等于 source_total
    if universe == "watchlist" and user_id is not None:
        stmt = select(UserWatchlistItem.instrument_id).where(
            UserWatchlistItem.user_id == user_id,
            UserWatchlistItem.active.is_(True),
        )
        result = await db.execute(stmt)
        watchlist_instrument_ids = {row[0] for row in result.all()}
        # universe_total: 自选股范围内的结果数（无指标过滤）
        universe_total = await count_by_run_with_watchlist(
            db, run_id, watchlist_instrument_ids
        )

    # 5. 执行服务端筛选/排序/分页（SQL 级 watchlist 过滤）
    offset = (page - 1) * page_size
    result_page = await query_results(
        db,
        run_id=run_id,
        filters=filters,
        sort=sort,
        watchlist_instrument_ids=watchlist_instrument_ids,
        keyword=keyword,
        limit=page_size,
        offset=offset,
    )

    return SelectorResultPage(
        run_id=run_id,
        strategy_key=definition.strategy_key,
        trade_date=run.trade_date,
        source_total=source_total,
        universe_total=universe_total,
        filtered_total=result_page.total,
        page=page,
        page_size=page_size,
        items=result_page.items,
    )


if __name__ == "__main__":
    # 自测入口：验证函数签名与数据类（不连 DB）
    assert issubclass(SelectorQueryError, Exception)
    assert issubclass(RunNotFoundError, SelectorQueryError)
    assert issubclass(NotSelectorRunError, SelectorQueryError)
    assert issubclass(InvalidFilterError, SelectorQueryError)
    print("异常层级正确 ✓")

    # 验证 SelectorResultPage
    from uuid import uuid4

    test_page = SelectorResultPage(
        run_id=uuid4(),
        strategy_key=DSA_SELECTOR,
        trade_date=date(2026, 6, 18),
        source_total=100,
        universe_total=100,
        filtered_total=30,
        page=1,
        page_size=50,
    )
    assert test_page.source_total == 100
    assert test_page.universe_total == 100
    assert test_page.filtered_total == 30
    assert test_page.items == []
    assert test_page.page == 1
    assert test_page.page_size == 50
    print("SelectorResultPage ✓")

    # 验证 query_published_selector_results 签名
    import inspect

    sig = inspect.signature(query_published_selector_results)
    params = list(sig.parameters.keys())
    assert params[0] == "db"
    for p in params[1:]:
        assert sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY, f"{p} 不是 keyword-only"
    assert "run_id" in sig.parameters
    assert "user_id" in sig.parameters
    assert "filters" in sig.parameters
    assert "sort" in sig.parameters
    assert "page" in sig.parameters
    assert "page_size" in sig.parameters
    assert "universe" in sig.parameters
    print("query_published_selector_results 签名正确 ✓")

    print("OK")
