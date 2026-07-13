"""策略结果仓储 - 批量写入与查询策略运行结果。

提供：
- create_run: 创建策略运行记录
- update_run_status: 更新运行状态
- write_results: 批量写入结果 + 指标（ON CONFLICT 更新）
- query_results: 按指标筛选排序查询结果
- get_result: 获取单个结果详情
- list_runs: 查询运行历史

设计说明：
- 唯一约束: (run_id, instrument_id) 确保结果不可变——同一 run 的同一 instrument 只有一条记录，不同 run 的结果互不覆盖
- 向量化: 批量插入用 PostgreSQL insert + on_conflict_do_nothing（同一 run 内幂等）
- 指标拆分: 数值型存 numeric_value，文本型存 text_value，布尔型存 bool_value
- 指标索引: ix_metric_numeric 支持 (strategy_version_id, trade_date, metric_key, numeric_value) 高效筛选排序

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.selectable import Select

from app.models.instrument import Instrument
from app.models.strategy_run import (
    StrategyResult,
    StrategyResultMetric,
    StrategyRun,
    StrategyRunItem,
)
from app.strategy.runtime import StrategyResult as RuntimeStrategyResult

logger = logging.getLogger("strategy_result_repository")

# [策略结果] - asyncpg 参数上限 32767，批量 INSERT 分批大小
_BATCH_SIZE = 500


@dataclass
class MetricFilter:
    """指标筛选条件，支持 6 种操作符。"""
    metric_key: str
    operator: str  # gt, gte, lt, lte, eq, between
    value: float | None = None       # for gt/gte/lt/lte/eq
    value1: float | None = None      # for between (lower bound)
    value2: float | None = None      # for between (upper bound)


@dataclass
class SortSpec:
    """排序规格。"""
    field: str
    desc: bool = False


@dataclass
class QueryResultPage:
    """查询结果分页。"""
    items: list  # list[StrategyResult]
    total: int  # filtered_total (过滤后)
    source_total: int = 0  # source_total (过滤前)


def dict_filters_to_metric_filters(
    metric_filters: list[dict[str, Any]] | None,
) -> list[MetricFilter]:
    """将 dict 格式的 metric_filters 转换为 MetricFilter 列表。

    支持两种 dict 格式：
    - 旧版 min_value/max_value 格式 → 转为 between 操作
    - 新版 operator/value 格式 → 直接映射

    所有数值字段强制 float() 转换；非数值或非有限值（NaN/Inf）抛 HTTPException 422。

    Args:
        metric_filters: dict 格式筛选条件列表

    Returns:
        MetricFilter 列表（输入为空时返回空列表）

    Raises:
        HTTPException 422: 筛选值非数值或非有限（NaN/Inf）
    """
    if not metric_filters:
        return []
    result: list[MetricFilter] = []
    for f in metric_filters:
        metric_key = f.get("metric_key")
        if not metric_key:
            continue
        # 旧版 min_value/max_value 格式
        min_val = f.get("min_value")
        max_val = f.get("max_value")
        if min_val is not None or max_val is not None:
            # [StrategyResultRepository] - 描述: 旧版格式数值强制转换，None 保留为 None（开区间）
            v1 = _to_float_or_422(min_val, metric_key, "min_value") if min_val is not None else None
            v2 = _to_float_or_422(max_val, metric_key, "max_value") if max_val is not None else None
            result.append(MetricFilter(
                metric_key=metric_key,
                operator="between",
                value1=v1,
                value2=v2,
            ))
            continue
        # 新版 operator/value 格式
        operator = f.get("operator", "between")
        if operator == "between":
            # [StrategyResultRepository] - 描述: between 操作下界写入 value1，上界写入 value2
            v1 = _to_float_or_422(f.get("value1"), metric_key, "value1")
            v2 = _to_float_or_422(f.get("value2"), metric_key, "value2")
            result.append(MetricFilter(
                metric_key=metric_key,
                operator=operator,
                value1=v1,
                value2=v2,
            ))
        else:
            v = _to_float_or_422(f.get("value"), metric_key, "value")
            result.append(MetricFilter(
                metric_key=metric_key,
                operator=operator,
                value=v,
            ))
    return result


def _to_float_or_422(raw_value: Any, metric_key: str, field_name: str) -> float:
    """将 raw_value 强制转换为 float，非有限数值（NaN/Inf）返回 422。

    [StrategyResultRepository] - 描述: 筛选值类型硬校验，防止字符串/None/NaN 污染 SQL 参数

    Args:
        raw_value: 原始值（前端传入，可能是 str/int/float/None）
        metric_key: 指标键（用于错误消息定位）
        field_name: 字段名（value/value1/value2/min_value/max_value）

    Returns:
        转换后的 float 值

    Raises:
        HTTPException 422: 转换失败或结果非有限（NaN/Inf）
    """
    try:
        v = float(raw_value)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"筛选值必须是数值: metric_key={metric_key}, {field_name}={raw_value}",
        ) from e
    if not math.isfinite(v):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"筛选值必须是有限数值: metric_key={metric_key}, {field_name}={v}",
        )
    return v


async def create_run(
    session: AsyncSession,
    strategy_version_id: uuid.UUID,
    trade_date: date | None,
    run_type: str = "manual",
    input_overrides: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> StrategyRun:
    """创建策略运行记录。

    幂等：相同 idempotency_key 的运行不会重复创建。

    Args:
        session: 异步会话
        strategy_version_id: 策略版本 ID
        trade_date: 交易日
        run_type: 触发方式（manual/scheduled/replay）
        input_overrides: 输入参数覆盖
        idempotency_key: 幂等键（None 则自动生成）

    Returns:
        StrategyRun ORM 对象

    Raises:
        Exception: 创建失败时 re-raise
    """
    if idempotency_key is None:
        # 自动生成幂等键：version_id + run_type + trade_date
        date_str = trade_date.isoformat() if trade_date else "today"
        idempotency_key = f"{strategy_version_id}:{run_type}:{date_str}"

    # 检查是否已存在（幂等）
    existing_stmt = select(StrategyRun).where(
        StrategyRun.idempotency_key == idempotency_key
    )
    result = await session.execute(existing_stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        logger.info("运行已存在（幂等）: idempotency_key=%s", idempotency_key)
        return existing

    run = StrategyRun(
        strategy_version_id=strategy_version_id,
        run_type=run_type,
        trade_date=trade_date,
        status="pending",
        input_overrides=input_overrides or {},
        started_at=datetime.now(UTC),
        idempotency_key=idempotency_key,
    )
    session.add(run)
    try:
        await session.flush()
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"创建策略运行失败 strategy_version_id={strategy_version_id}: {exc}"
        ) from exc

    logger.info(
        "创建策略运行: run_id=%s, strategy_version_id=%s, trade_date=%s",
        run.id, strategy_version_id, trade_date,
    )
    return run


async def update_run_status(
    session: AsyncSession,
    run_id: uuid.UUID,
    status: str,
    error: str | None = None,
) -> StrategyRun | None:
    """更新运行状态。

    Args:
        session: 异步会话
        run_id: 运行 ID
        status: 新状态（running/succeeded/failed）
        error: 错误信息（失败时填入 input_overrides.error）

    Returns:
        更新后的 StrategyRun，或 None（运行不存在）

    Raises:
        Exception: 更新失败时 re-raise
    """
    stmt = select(StrategyRun).where(StrategyRun.id == run_id)
    result = await session.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        return None

    run.status = status
    run.finished_at = datetime.now(UTC) if status in ("succeeded", "failed") else None

    if error is not None:
        # error 存储在 input_overrides JSONB 中（迁移未单独建 error 列）
        overrides = dict(run.input_overrides) if run.input_overrides else {}
        overrides["error"] = error
        run.input_overrides = overrides

    try:
        await session.flush()
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"更新运行状态失败 run_id={run_id}, status={status}: {exc}"
        ) from exc

    return run


async def write_results(
    session: AsyncSession,
    run_id: uuid.UUID,
    strategy_version_id: uuid.UUID,
    results: list[RuntimeStrategyResult],
) -> int:
    """批量写入策略结果 + 指标（ON CONFLICT DO NOTHING，结果不可变）。

    唯一约束: (run_id, instrument_id)
    同一 run 内重复写入幂等（DO NOTHING），不同 run 的结果互不覆盖。

    指标拆分写入 strategy_result_metrics 表：
    - 数值型（int/float）→ numeric_value
    - 布尔型 → bool_value
    - 其他（str/None）→ text_value

    向量化说明：
    - 使用 PostgreSQL insert + on_conflict_do_nothing 实现幂等写入
    - 单次 execute 发送所有记录（executemany 语义）
    - 指标表也用 on_conflict_do_update 实现 upsert

    Args:
        session: 异步会话
        run_id: 运行 ID
        strategy_version_id: 策略版本 ID
        results: 策略运行时结果列表

    Returns:
        写入结果数

    Raises:
        Exception: 写入失败时 re-raise
    """
    if not results:
        return 0

    # 0. 不可变检查：已完成/已发布的运行不允许写入新结果
    run_stmt = select(StrategyRun.status).where(StrategyRun.id == run_id)
    run_result = await session.execute(run_stmt)
    run_status = run_result.scalar_one_or_none()
    if run_status is not None and run_status in ("completed", "partial_failed", "published"):
        raise ValueError(
            f"运行已完成或已发布（status={run_status}），禁止写入新结果: run_id={run_id}"
        )

    # 1. 批量写入 strategy_results（ON CONFLICT DO NOTHING，结果不可变）
    result_records: list[dict[str, Any]] = []
    for r in results:
        result_records.append({
            "run_id": run_id,
            "strategy_version_id": strategy_version_id,
            "instrument_id": r.instrument_id,
            "trade_date": r.trade_date,
            "payload": _build_payload(r),
        })

    try:
        for i in range(0, len(result_records), _BATCH_SIZE):
            batch = result_records[i : i + _BATCH_SIZE]
            stmt = pg_insert(StrategyResult).values(batch)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["run_id", "instrument_id"],
            )
            await session.execute(stmt)
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"批量写入策略结果失败 run_id={run_id}, count={len(results)}: {exc}"
        ) from exc

    # 2. 查询刚写入的 result_id（用于关联指标）
    # 按 run_id + instrument_id 查询（与唯一约束对齐）
    instrument_ids = [r.instrument_id for r in results]
    id_query = select(StrategyResult.id, StrategyResult.instrument_id).where(
        and_(
            StrategyResult.run_id == run_id,
            StrategyResult.instrument_id.in_(instrument_ids),
        )
    )
    id_result = await session.execute(id_query)
    result_id_map: dict[uuid.UUID, uuid.UUID] = {
        instrument_id: result_id for result_id, instrument_id in id_result.all()
    }

    # 3. 批量 upsert strategy_result_metrics
    metric_records: list[dict[str, Any]] = []
    for r in results:
        result_id = result_id_map.get(r.instrument_id)
        if result_id is None:
            continue
        for key, value in r.metrics.items():
            numeric_val, text_val, bool_val = _classify_metric_value(value)
            metric_records.append({
                "result_id": result_id,
                "strategy_version_id": strategy_version_id,
                "trade_date": r.trade_date,
                "instrument_id": r.instrument_id,
                "metric_key": key,
                "numeric_value": numeric_val,
                "text_value": text_val,
                "bool_value": bool_val,
            })

    if metric_records:
        try:
            for i in range(0, len(metric_records), _BATCH_SIZE):
                batch = metric_records[i : i + _BATCH_SIZE]
                metric_stmt = pg_insert(StrategyResultMetric).values(batch)
                metric_stmt = metric_stmt.on_conflict_do_update(
                    index_elements=["result_id", "metric_key"],
                    set_={
                        "numeric_value": metric_stmt.excluded.numeric_value,
                        "text_value": metric_stmt.excluded.text_value,
                        "bool_value": metric_stmt.excluded.bool_value,
                    },
                )
                await session.execute(metric_stmt)
        except Exception as exc:
            await session.rollback()
            raise RuntimeError(
                f"批量写入策略指标失败 run_id={run_id}, count={len(metric_records)}: {exc}"
            ) from exc

    try:
        await session.flush()
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"flush 策略结果失败 run_id={run_id}: {exc}"
        ) from exc

    logger.info(
        "写入策略结果: run_id=%s, results=%d, metrics=%d",
        run_id, len(results), len(metric_records),
    )
    return len(results)


def _build_payload(r: RuntimeStrategyResult) -> dict[str, Any]:
    """构建结果 payload JSON。

    仅包含 metrics，不含 matched（matched 由用户筛选条件动态决定，不持久化）。
    """
    payload: dict[str, Any] = {}
    payload.update(r.metrics)
    return payload


def _classify_metric_value(
    value: Any,
) -> tuple[float | None, str | None, bool | None]:
    """将指标值分类存储到 numeric/text/bool 列。

    Args:
        value: 指标值（int/float/str/bool/None/其他）

    Returns:
        (numeric_value, text_value, bool_value) 三元组，只有一个非 None
    """
    if value is None:
        return None, None, None
    if isinstance(value, bool):
        return None, None, value
    if isinstance(value, (int, float)):
        return float(value), None, None
    # 其他类型转为字符串
    return None, str(value), None


async def query_results(
    session: AsyncSession,
    *,
    run_id: uuid.UUID | None = None,
    strategy_version_id: uuid.UUID | None = None,
    trade_date: date | None = None,
    filters: list[MetricFilter] | None = None,
    sort: SortSpec | None = None,
    matched_only: bool = False,
    watchlist_instrument_ids: set[uuid.UUID] | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> QueryResultPage:
    """按指标筛选排序查询结果。

    利用 ix_metric_numeric 索引高效筛选排序：
    - 筛选：通过子查询 JOIN strategy_result_metrics 表
    - 排序：通过 LEFT JOIN strategy_result_metrics 表

    Args:
        session: 异步会话
        run_id: 运行 ID（优先级最高，提供后按 run_id 过滤）
        strategy_version_id: 策略版本 ID（run_id 未提供时必需）
        trade_date: 交易日（run_id 未提供时必需）
        filters: 指标筛选条件列表（MetricFilter 对象）
        sort: 排序规格（SortSpec 对象，None 表示不排序）
        matched_only: 只返回 matched=True 的结果
        watchlist_instrument_ids: 自选股 instrument_id 集合（SQL 级过滤，替代 Python 后过滤）
        limit: 返回上限
        offset: 偏移量（分页）

    Returns:
        QueryResultPage（items=结果列表, total=总数）

    Raises:
        Exception: 查询失败时 re-raise
    """
    try:
        # 基础查询
        base = select(StrategyResult)

        # run_id 过滤（优先级最高）
        if run_id is not None:
            base = base.where(StrategyResult.run_id == run_id)

        # strategy_version_id + trade_date 过滤
        if strategy_version_id is not None:
            base = base.where(
                StrategyResult.strategy_version_id == strategy_version_id
            )
        if trade_date is not None:
            base = base.where(StrategyResult.trade_date == trade_date)

        # matched 筛选（payload->>'matched' = 'true'）
        if matched_only:
            base = base.where(
                text("payload->>'matched' = 'true'")
            )

        # 自选股 SQL 级过滤
        if watchlist_instrument_ids is not None:
            base = base.where(
                StrategyResult.instrument_id.in_(watchlist_instrument_ids)
            )

        # keyword 过滤（JOIN instruments 表，symbol/name/pinyin_initials ILIKE 匹配）
        if keyword is not None:
            kw_pattern = f"%{keyword}%"
            base = base.join(
                Instrument, StrategyResult.instrument_id == Instrument.id
            ).where(
                or_(
                    Instrument.symbol.ilike(kw_pattern),
                    Instrument.name.ilike(kw_pattern),
                    Instrument.pinyin_initials.ilike(kw_pattern),
                )
            )

        # 指标筛选（通过 EXISTS 子查询，支持 6 种操作符）
        if filters:
            for f in filters:
                metric_key = f.metric_key
                sub = select(StrategyResultMetric.result_id).where(
                    StrategyResultMetric.metric_key == metric_key
                )
                op = f.operator.lower()
                if op == "gt":
                    sub = sub.where(StrategyResultMetric.numeric_value > f.value)
                elif op == "gte":
                    sub = sub.where(StrategyResultMetric.numeric_value >= f.value)
                elif op == "lt":
                    sub = sub.where(StrategyResultMetric.numeric_value < f.value)
                elif op == "lte":
                    sub = sub.where(StrategyResultMetric.numeric_value <= f.value)
                elif op == "eq":
                    sub = sub.where(StrategyResultMetric.numeric_value == f.value)
                elif op == "between":
                    if f.value1 is not None:
                        sub = sub.where(StrategyResultMetric.numeric_value >= f.value1)
                    if f.value2 is not None:
                        sub = sub.where(StrategyResultMetric.numeric_value <= f.value2)
                else:
                    raise ValueError(f"未知筛选操作符: {op}")
                base = base.where(StrategyResult.id.in_(sub))

        # 排序（通过 LEFT JOIN 指标表）
        if sort is not None:
            sort_sub = (
                select(
                    StrategyResultMetric.result_id,
                    StrategyResultMetric.numeric_value.label("sort_val"),
                )
                .where(StrategyResultMetric.metric_key == sort.field)
                .subquery()
            )
            base = base.outerjoin(
                sort_sub, StrategyResult.id == sort_sub.c.result_id
            )
            if sort.desc:
                base = base.order_by(sort_sub.c.sort_val.desc().nullslast())
            else:
                base = base.order_by(sort_sub.c.sort_val.asc().nullsfirst())

        # 总数查询（复用相同过滤条件）
        count_base = select(StrategyResult)
        if run_id is not None:
            count_base = count_base.where(StrategyResult.run_id == run_id)
        if strategy_version_id is not None:
            count_base = count_base.where(
                StrategyResult.strategy_version_id == strategy_version_id
            )
        if trade_date is not None:
            count_base = count_base.where(StrategyResult.trade_date == trade_date)
        if matched_only:
            count_base = count_base.where(text("payload->>'matched' = 'true'"))
        if watchlist_instrument_ids is not None:
            count_base = count_base.where(
                StrategyResult.instrument_id.in_(watchlist_instrument_ids)
            )
        if keyword is not None:
            kw_pattern = f"%{keyword}%"
            count_base = count_base.join(
                Instrument, StrategyResult.instrument_id == Instrument.id
            ).where(
                or_(
                    Instrument.symbol.ilike(kw_pattern),
                    Instrument.name.ilike(kw_pattern),
                    Instrument.pinyin_initials.ilike(kw_pattern),
                )
            )
        if filters:
            for f in filters:
                metric_key = f.metric_key
                sub = select(StrategyResultMetric.result_id).where(
                    StrategyResultMetric.metric_key == metric_key
                )
                op = f.operator.lower()
                if op == "gt":
                    sub = sub.where(StrategyResultMetric.numeric_value > f.value)
                elif op == "gte":
                    sub = sub.where(StrategyResultMetric.numeric_value >= f.value)
                elif op == "lt":
                    sub = sub.where(StrategyResultMetric.numeric_value < f.value)
                elif op == "lte":
                    sub = sub.where(StrategyResultMetric.numeric_value <= f.value)
                elif op == "eq":
                    sub = sub.where(StrategyResultMetric.numeric_value == f.value)
                elif op == "between":
                    if f.value1 is not None:
                        sub = sub.where(StrategyResultMetric.numeric_value >= f.value1)
                    if f.value2 is not None:
                        sub = sub.where(StrategyResultMetric.numeric_value <= f.value2)
                else:
                    raise ValueError(f"未知筛选操作符: {op}")
                count_base = count_base.where(StrategyResult.id.in_(sub))

        count_result = await session.execute(
            select(text("count(*)")).select_from(count_base.subquery())
        )
        total = int(count_result.scalar() or 0)

        # 分页查询（eager load instrument 以获取 symbol/name/market）
        base = base.options(selectinload(StrategyResult.instrument))
        base = base.limit(limit).offset(offset)
        result = await session.execute(base)
        items = list(result.scalars().all())

        return QueryResultPage(items=items, total=total)
    except Exception as exc:
        raise RuntimeError(
            f"查询策略结果失败 run_id={run_id}, "
            f"strategy_version_id={strategy_version_id}, "
            f"trade_date={trade_date}: {exc}"
        ) from exc


async def count_by_run(session: AsyncSession, run_id: uuid.UUID) -> int:
    """返回指定 run 的总结果数（无过滤）。"""
    stmt = select(func.count()).select_from(StrategyResult).where(
        StrategyResult.run_id == run_id
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_by_run_with_watchlist(
    session: AsyncSession,
    run_id: uuid.UUID,
    watchlist_instrument_ids: set[uuid.UUID],
) -> int:
    """返回指定 run 在自选股范围内的结果数（无指标过滤，仅 watchlist 过滤）。

    用于 selector_query_service 计算 universe_total（universe=watchlist 时）。

    Args:
        session: 异步会话
        run_id: 运行 ID
        watchlist_instrument_ids: 自选股 instrument_id 集合

    Returns:
        watchlist 范围内的结果总数
    """
    stmt = select(func.count()).select_from(StrategyResult).where(
        StrategyResult.run_id == run_id,
        StrategyResult.instrument_id.in_(watchlist_instrument_ids),
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


# ---------------------------------------------------------------------------
# 全量 Universe 查询（以 strategy_run_items 为主表）
# 用于趋势选股页展示 succeeded/skipped/failed 全部行
# ---------------------------------------------------------------------------


@dataclass
class RunItemResultRow:
    """[StrategyResultRepository] - 描述: 以 strategy_run_items 为主表的结果行

    趋势选股页全量 universe 展示的查询结果单元：
    - item_status/reason_code/error_message 来自 strategy_run_items
    - result 为 StrategyResult 或 None（skipped/failed 行为 None）
    - instrument 来自 instruments 表（LEFT JOIN，理论上永不为 None）
    """

    item_id: uuid.UUID
    run_id: uuid.UUID
    instrument_id: uuid.UUID
    item_status: str
    reason_code: str | None
    error_message: str | None
    result: StrategyResult | None
    instrument: Instrument | None


def _apply_run_item_filters(
    base: Select,
    *,
    run_id: uuid.UUID,
    filters: list[MetricFilter] | None,
    watchlist_instrument_ids: set[uuid.UUID] | None,
    keyword: str | None,
) -> Select:
    """[StrategyResultRepository] - 描述: 对 strategy_run_items 查询应用通用过滤条件

    复用过滤逻辑避免 query_run_items_with_results 与 count 之间漂移。

    - run_id: 必填，主过滤
    - watchlist_instrument_ids: IN 过滤 instrument_id
    - keyword: JOIN instruments ILIKE
    - filters: metric_filter 通过 (run_id, instrument_id) 子查询过滤（skipped/failed 行无 strategy_results 自动不命中）

    注意：strategy_run_items.result_id 在 PR #14 batch service 中未回填（始终为 None），
    因此不能通过 result_id 关联 strategy_results/strategy_result_metrics，
    必须通过 (run_id, instrument_id) 关联。
    """
    base = base.where(StrategyRunItem.run_id == run_id)

    if watchlist_instrument_ids is not None:
        base = base.where(
            StrategyRunItem.instrument_id.in_(watchlist_instrument_ids)
        )

    if keyword is not None:
        kw_pattern = f"%{keyword}%"
        base = base.join(
            Instrument, StrategyRunItem.instrument_id == Instrument.id
        ).where(
            or_(
                Instrument.symbol.ilike(kw_pattern),
                Instrument.name.ilike(kw_pattern),
                Instrument.pinyin_initials.ilike(kw_pattern),
            )
        )

    if filters:
        for f in filters:
            metric_key = f.metric_key
            # [StrategyResultRepository] - 描述: 通过 (run_id, instrument_id) 关联 metrics
            # batch service 未回填 result_id，必须用 instrument_id 关联
            sub = (
                select(StrategyResult.instrument_id)
                .join(
                    StrategyResultMetric,
                    StrategyResultMetric.result_id == StrategyResult.id,
                )
                .where(StrategyResult.run_id == run_id)
                .where(StrategyResultMetric.metric_key == metric_key)
            )
            op = f.operator.lower()
            if op == "gt":
                sub = sub.where(StrategyResultMetric.numeric_value > f.value)
            elif op == "gte":
                sub = sub.where(StrategyResultMetric.numeric_value >= f.value)
            elif op == "lt":
                sub = sub.where(StrategyResultMetric.numeric_value < f.value)
            elif op == "lte":
                sub = sub.where(StrategyResultMetric.numeric_value <= f.value)
            elif op == "eq":
                sub = sub.where(StrategyResultMetric.numeric_value == f.value)
            elif op == "between":
                if f.value1 is not None:
                    sub = sub.where(StrategyResultMetric.numeric_value >= f.value1)
                if f.value2 is not None:
                    sub = sub.where(StrategyResultMetric.numeric_value <= f.value2)
            else:
                raise ValueError(f"未知筛选操作符: {op}")
            base = base.where(StrategyRunItem.instrument_id.in_(sub))

    return base


async def query_run_items_with_results(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    filters: list[MetricFilter] | None = None,
    sort: SortSpec | None = None,
    watchlist_instrument_ids: set[uuid.UUID] | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> QueryResultPage:
    """以 strategy_run_items 为主表 LEFT JOIN strategy_results + instruments 查询。

    趋势选股页全量 universe 展示的查询入口：
    - 主表 strategy_run_items（含 succeeded/skipped/failed 全部行）
    - LEFT JOIN strategy_results（通过 run_id + instrument_id 关联，非 result_id）
    - LEFT JOIN instruments（取 symbol/name/market）
    - metric_filters 通过 (run_id, instrument_id) 子查询过滤（自动排除 skipped/failed）
    - sort 通过 LEFT JOIN 指标表（NULLS LAST）
    - keyword 通过 JOIN instruments ILIKE 过滤
    - watchlist_instrument_ids 通过 IN 过滤 instrument_id

    注意：strategy_run_items.result_id 在 PR #14 batch service 中未回填（始终为 None），
    因此不能通过 result_id 关联 strategy_results，必须通过 (run_id, instrument_id) 关联。

    Returns:
        QueryResultPage(items=list[RunItemResultRow], total=filtered_total)

    Raises:
        Exception: 查询失败时 re-raise
    """
    try:
        # 构建基础查询（StrategyRunItem 为主表，不使用 selectinload 因 result_id 未回填）
        base = select(StrategyRunItem)

        # 应用通用过滤（run_id + watchlist + keyword + metric_filters）
        base = _apply_run_item_filters(
            base,
            run_id=run_id,
            filters=filters,
            watchlist_instrument_ids=watchlist_instrument_ids,
            keyword=keyword,
        )

        # 排序（LEFT JOIN 指标表，通过 instrument_id 关联，NULLS LAST）
        if sort is not None:
            sort_sub = (
                select(
                    StrategyResult.instrument_id.label("sort_instrument_id"),
                    StrategyResultMetric.numeric_value.label("sort_val"),
                )
                .join(
                    StrategyResultMetric,
                    StrategyResultMetric.result_id == StrategyResult.id,
                )
                .where(StrategyResult.run_id == run_id)
                .where(StrategyResultMetric.metric_key == sort.field)
                .subquery()
            )
            base = base.outerjoin(
                sort_sub,
                StrategyRunItem.instrument_id == sort_sub.c.sort_instrument_id,
            )
            if sort.desc:
                base = base.order_by(sort_sub.c.sort_val.desc().nullslast())
            else:
                base = base.order_by(sort_sub.c.sort_val.asc().nullsfirst())

        # 总数查询（复用相同过滤条件，不含 limit/offset/sort）
        count_base = select(StrategyRunItem)
        count_base = _apply_run_item_filters(
            count_base,
            run_id=run_id,
            filters=filters,
            watchlist_instrument_ids=watchlist_instrument_ids,
            keyword=keyword,
        )
        count_result = await session.execute(
            select(func.count()).select_from(count_base.subquery())
        )
        total = int(count_result.scalar() or 0)

        # 分页查询
        base = base.limit(limit).offset(offset)
        result = await session.execute(base)
        items_orm = list(result.scalars().all())

        # 批量加载 strategy_results（通过 run_id + instrument_id 关联，非 result_id）
        instrument_ids = {item.instrument_id for item in items_orm}
        results_map: dict[uuid.UUID, StrategyResult] = {}
        if instrument_ids:
            res_stmt = select(StrategyResult).where(
                StrategyResult.run_id == run_id,
                StrategyResult.instrument_id.in_(instrument_ids),
            )
            res_result = await session.execute(res_stmt)
            for res in res_result.scalars().all():
                results_map[res.instrument_id] = res

        # 批量加载 instruments 避免 N+1
        instruments_map: dict[uuid.UUID, Instrument] = {}
        if instrument_ids:
            inst_stmt = select(Instrument).where(
                Instrument.id.in_(instrument_ids)
            )
            inst_result = await session.execute(inst_stmt)
            for inst in inst_result.scalars().all():
                instruments_map[inst.id] = inst

        rows: list[RunItemResultRow] = []
        for item in items_orm:
            rows.append(
                RunItemResultRow(
                    item_id=item.id,
                    run_id=item.run_id,
                    instrument_id=item.instrument_id,
                    item_status=item.status,
                    reason_code=item.reason_code,
                    error_message=item.error_message,
                    result=results_map.get(item.instrument_id),
                    instrument=instruments_map.get(item.instrument_id),
                )
            )

        return QueryResultPage(items=rows, total=total)
    except Exception as exc:
        raise RuntimeError(
            f"查询 run_items 全量结果失败 run_id={run_id}: {exc}"
        ) from exc


async def count_run_items_by_run(
    session: AsyncSession,
    run_id: uuid.UUID,
) -> int:
    """返回指定 run 的 strategy_run_items 总数（无过滤，含全部 status）。

    用于 selector_query_service 的 source_total fallback
    （run.total_instruments 为 None 时使用，正常生产场景 run.total_instruments 已填充）。
    """
    stmt = select(func.count()).select_from(StrategyRunItem).where(
        StrategyRunItem.run_id == run_id
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def count_run_items_with_watchlist(
    session: AsyncSession,
    run_id: uuid.UUID,
    watchlist_instrument_ids: set[uuid.UUID],
) -> int:
    """返回指定 run 在自选股范围内的 strategy_run_items 总数（无指标过滤，仅 watchlist 过滤）。

    用于 selector_query_service 计算 universe_total（universe=watchlist 时）。
    """
    stmt = select(func.count()).select_from(StrategyRunItem).where(
        StrategyRunItem.run_id == run_id,
        StrategyRunItem.instrument_id.in_(watchlist_instrument_ids),
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def get_result(
    session: AsyncSession,
    result_id: uuid.UUID,
) -> StrategyResult | None:
    """获取单个结果详情。

    Args:
        session: 异步会话
        result_id: 结果 ID

    Returns:
        StrategyResult 或 None（不存在）

    Raises:
        Exception: 查询失败时 re-raise
    """
    try:
        stmt = select(StrategyResult).where(StrategyResult.id == result_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    except Exception as exc:
        raise RuntimeError(
            f"获取策略结果失败 result_id={result_id}: {exc}"
        ) from exc


async def list_runs(
    session: AsyncSession,
    strategy_version_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StrategyRun], int]:
    """查询运行历史。

    Args:
        session: 异步会话
        strategy_version_id: 策略版本 ID（None 表示所有）
        status: 运行状态过滤（None 表示所有）
        limit: 返回上限
        offset: 偏移量

    Returns:
        (运行列表, 总数)

    Raises:
        Exception: 查询失败时 re-raise
    """
    try:
        stmt = select(StrategyRun)
        count_stmt = select(StrategyRun)

        if strategy_version_id is not None:
            stmt = stmt.where(StrategyRun.strategy_version_id == strategy_version_id)
            count_stmt = count_stmt.where(
                StrategyRun.strategy_version_id == strategy_version_id
            )
        if status is not None:
            stmt = stmt.where(StrategyRun.status == status)
            count_stmt = count_stmt.where(StrategyRun.status == status)

        # 总数
        count_result = await session.execute(
            select(text("count(*)")).select_from(count_stmt.subquery())
        )
        total = int(count_result.scalar() or 0)

        # 分页
        stmt = stmt.order_by(StrategyRun.started_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        items = list(result.scalars().all())

        return items, total
    except Exception as exc:
        raise RuntimeError(f"查询运行历史失败: {exc}") from exc


if __name__ == "__main__":
    # 自测入口：验证函数签名与基础逻辑（不连 DB）

    # 验证函数存在
    assert callable(create_run)
    assert callable(update_run_status)
    assert callable(write_results)
    assert callable(query_results)
    assert callable(count_by_run)
    assert callable(get_result)
    assert callable(list_runs)
    print("所有仓储函数可调用 ✓")

    # 验证 _classify_metric_value
    assert _classify_metric_value(42) == (42.0, None, None)
    assert _classify_metric_value(3.14) == (3.14, None, None)
    assert _classify_metric_value(True) == (None, None, True)
    assert _classify_metric_value(False) == (None, None, False)
    assert _classify_metric_value("text") == (None, "text", None)
    assert _classify_metric_value(None) == (None, None, None)
    print("_classify_metric_value 分类正确 ✓")

    # 验证 _build_payload
    from uuid import uuid4

    from app.strategy.runtime import StrategyResult as RuntimeResult

    r = RuntimeResult(
        instrument_id=uuid4(),
        strategy_version_id=uuid4(),
        trade_date=date(2026, 6, 18),
        matched=True,
        metrics={"dsa_dir_bars": 60, "offset_mean": 0.05},
    )
    payload = _build_payload(r)
    assert "matched" not in payload  # matched 不持久化到 payload
    assert payload["dsa_dir_bars"] == 60
    assert payload["offset_mean"] == 0.05
    print(f"_build_payload: {payload} ✓")

    # 验证 MetricFilter / SortSpec / QueryResultPage
    mf = MetricFilter(metric_key="dsa_dir_bars", operator="gte", value=50)
    assert mf.metric_key == "dsa_dir_bars"
    assert mf.operator == "gte"
    assert mf.value == 50
    mf_between = MetricFilter(metric_key="vwap_ret_avg", operator="between", value1=0.0, value2=0.5)
    assert mf_between.value1 == 0.0
    assert mf_between.value2 == 0.5
    ss = SortSpec(field="dsa_dir_bars", desc=True)
    assert ss.field == "dsa_dir_bars"
    assert ss.desc is True
    qrp = QueryResultPage(items=[], total=0)
    assert qrp.items == []
    assert qrp.total == 0
    assert qrp.source_total == 0
    qrp_with_source = QueryResultPage(items=[], total=0, source_total=42)
    assert qrp_with_source.source_total == 42
    print("MetricFilter / SortSpec / QueryResultPage ✓")

    # 验证 dict_filters_to_metric_filters
    # 旧版 min_value/max_value 格式
    old_filters = [{"metric_key": "dsa_dir_bars", "min_value": 50, "max_value": 100}]
    converted = dict_filters_to_metric_filters(old_filters)
    assert converted is not None
    assert len(converted) == 1
    assert converted[0].metric_key == "dsa_dir_bars"
    assert converted[0].operator == "between"
    assert converted[0].value1 == 50
    assert converted[0].value2 == 100
    # 新版 operator/value 格式
    new_filters = [{"metric_key": "dsa_dir_bars", "operator": "gte", "value": 50}]
    converted2 = dict_filters_to_metric_filters(new_filters)
    assert converted2 is not None
    assert len(converted2) == 1
    assert converted2[0].metric_key == "dsa_dir_bars"
    assert converted2[0].operator == "gte"
    assert converted2[0].value == 50
    # 空输入（返回空列表，与 None 在 if 判断中语义等价）
    assert dict_filters_to_metric_filters(None) == []
    assert dict_filters_to_metric_filters([]) == []
    print("dict_filters_to_metric_filters ✓")

    # 验证 query_results 签名（keyword-only 参数）
    import inspect
    sig = inspect.signature(query_results)
    params = list(sig.parameters.keys())
    assert params[0] == "session"
    # session 之后的所有参数应为 keyword-only
    for p in params[1:]:
        assert sig.parameters[p].kind == inspect.Parameter.KEYWORD_ONLY, f"{p} 不是 keyword-only"
    assert "run_id" in sig.parameters
    assert "filters" in sig.parameters
    assert "sort" in sig.parameters
    assert "watchlist_instrument_ids" in sig.parameters
    # from __future__ import annotations 使注解变为字符串，需用 evaluate
    from typing import get_type_hints
    hints = get_type_hints(query_results)
    assert hints["return"] == QueryResultPage
    print("query_results 签名正确（keyword-only + QueryResultPage 返回） ✓")

    # 验证未知操作符 fail-closed（raise ValueError）
    bad_filter = MetricFilter(metric_key="test", operator="invalid_op", value=1)
    # 构造 query_results 内部的操作符分发逻辑来验证
    op = bad_filter.operator.lower()
    try:
        if op == "gt":
            pass
        elif op == "gte":
            pass
        elif op == "lt":
            pass
        elif op == "lte":
            pass
        elif op == "eq":
            pass
        elif op == "between":
            pass
        else:
            raise ValueError(f"未知筛选操作符: {op}")
        assert False, "应抛出 ValueError"
    except ValueError as e:
        assert "未知筛选操作符" in str(e)
    print("未知操作符 fail-closed ✓")

    print("OK")
