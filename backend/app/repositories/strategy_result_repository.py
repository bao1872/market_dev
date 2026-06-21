"""策略结果仓储 - 批量写入与查询策略运行结果。

提供：
- create_run: 创建策略运行记录
- update_run_status: 更新运行状态
- write_results: 批量写入结果 + 指标（ON CONFLICT 更新）
- query_results: 按指标筛选排序查询结果
- get_result: 获取单个结果详情
- list_runs: 查询运行历史

设计说明：
- 唯一约束: strategy_version + trade_date + instrument（重复写入用 ON CONFLICT 更新）
- 向量化: 批量插入用 PostgreSQL insert + on_conflict_do_update（executemany 语义）
- 指标拆分: 数值型存 numeric_value，文本型存 text_value，布尔型存 bool_value
- 指标索引: ix_metric_numeric 支持 (strategy_version_id, trade_date, metric_key, numeric_value) 高效筛选排序

禁异常吞没：所有异常补充上下文后 re-raise。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import and_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy_run import (
    StrategyResult,
    StrategyResultMetric,
    StrategyRun,
)
from app.strategy.runtime import StrategyResult as RuntimeStrategyResult

logger = logging.getLogger("strategy_result_repository")


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
    """批量写入策略结果 + 指标（ON CONFLICT 更新）。

    唯一约束: (strategy_version_id, trade_date, instrument_id)
    重复写入时用 ON CONFLICT DO UPDATE 更新 payload 和 run_id。

    指标拆分写入 strategy_result_metrics 表：
    - 数值型（int/float）→ numeric_value
    - 布尔型 → bool_value
    - 其他（str/None）→ text_value

    向量化说明：
    - 使用 PostgreSQL insert + on_conflict_do_update 实现批量 upsert
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

    # 1. 批量 upsert strategy_results
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
        stmt = pg_insert(StrategyResult).values(result_records)
        stmt = stmt.on_conflict_do_update(
            index_elements=["strategy_version_id", "trade_date", "instrument_id"],
            set_={
                "run_id": stmt.excluded.run_id,
                "payload": stmt.excluded.payload,
            },
        )
        result = await session.execute(stmt)
        _ = result.rowcount  # upsert 行数（不直接使用，后续通过查询获取 result_id）
    except Exception as exc:
        await session.rollback()
        raise RuntimeError(
            f"批量写入策略结果失败 run_id={run_id}, count={len(results)}: {exc}"
        ) from exc

    # 2. 查询刚写入的 result_id（用于关联指标）
    # 由于 upsert 可能是更新而非插入，需要查询所有相关 result_id
    instrument_ids = [r.instrument_id for r in results]
    trade_dates = list({r.trade_date for r in results})
    id_query = select(StrategyResult.id, StrategyResult.instrument_id).where(
        and_(
            StrategyResult.strategy_version_id == strategy_version_id,
            StrategyResult.trade_date.in_(trade_dates),
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
            metric_stmt = pg_insert(StrategyResultMetric).values(metric_records)
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
    strategy_version_id: uuid.UUID,
    trade_date: date,
    metric_filters: list[dict[str, Any]] | None = None,
    sort_by: str | None = None,
    sort_desc: bool = False,
    matched_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[StrategyResult], int]:
    """按指标筛选排序查询结果。

    利用 ix_metric_numeric 索引高效筛选排序：
    - 筛选：通过子查询 JOIN strategy_result_metrics 表
    - 排序：通过 LEFT JOIN strategy_result_metrics 表

    Args:
        session: 异步会话
        strategy_version_id: 策略版本 ID
        trade_date: 交易日
        metric_filters: 指标筛选条件列表，每项含 metric_key/min_value/max_value
        sort_by: 排序指标名（None 表示不排序）
        sort_desc: 是否降序
        matched_only: 只返回 matched=True 的结果
        limit: 返回上限
        offset: 偏移量（分页）

    Returns:
        (结果列表, 总数)

    Raises:
        Exception: 查询失败时 re-raise
    """
    try:
        # 基础查询
        base = select(StrategyResult).where(
            and_(
                StrategyResult.strategy_version_id == strategy_version_id,
                StrategyResult.trade_date == trade_date,
            )
        )

        # matched 筛选（payload->>'matched' = 'true'）
        if matched_only:
            base = base.where(
                text("payload->>'matched' = 'true'")
            )

        # 指标筛选（通过 EXISTS 子查询）
        if metric_filters:
            for f in metric_filters:
                metric_key = f.get("metric_key")
                min_val = f.get("min_value")
                max_val = f.get("max_value")
                if metric_key is None:
                    continue
                sub = select(StrategyResultMetric.result_id).where(
                    StrategyResultMetric.metric_key == metric_key
                )
                if min_val is not None:
                    sub = sub.where(StrategyResultMetric.numeric_value >= min_val)
                if max_val is not None:
                    sub = sub.where(StrategyResultMetric.numeric_value <= max_val)
                base = base.where(StrategyResult.id.in_(sub))

        # 排序（通过 LEFT JOIN 指标表）
        if sort_by:
            # 使用子查询获取排序值
            sort_sub = (
                select(
                    StrategyResultMetric.result_id,
                    StrategyResultMetric.numeric_value.label("sort_val"),
                )
                .where(StrategyResultMetric.metric_key == sort_by)
                .subquery()
            )
            base = base.outerjoin(
                sort_sub, StrategyResult.id == sort_sub.c.result_id
            )
            if sort_desc:
                base = base.order_by(sort_sub.c.sort_val.desc().nullslast())
            else:
                base = base.order_by(sort_sub.c.sort_val.asc().nullsfirst())

        # 总数查询
        count_stmt = select(StrategyResult).where(
            and_(
                StrategyResult.strategy_version_id == strategy_version_id,
                StrategyResult.trade_date == trade_date,
            )
        )
        if matched_only:
            count_stmt = count_stmt.where(text("payload->>'matched' = 'true'"))
        if metric_filters:
            for f in metric_filters:
                metric_key = f.get("metric_key")
                min_val = f.get("min_value")
                max_val = f.get("max_value")
                if metric_key is None:
                    continue
                sub = select(StrategyResultMetric.result_id).where(
                    StrategyResultMetric.metric_key == metric_key
                )
                if min_val is not None:
                    sub = sub.where(StrategyResultMetric.numeric_value >= min_val)
                if max_val is not None:
                    sub = sub.where(StrategyResultMetric.numeric_value <= max_val)
                count_stmt = count_stmt.where(StrategyResult.id.in_(sub))

        count_result = await session.execute(
            select(text("count(*)")).select_from(count_stmt.subquery())
        )
        total = int(count_result.scalar() or 0)

        # 分页查询
        base = base.limit(limit).offset(offset)
        result = await session.execute(base)
        items = list(result.scalars().all())

        return items, total
    except Exception as exc:
        raise RuntimeError(
            f"查询策略结果失败 strategy_version_id={strategy_version_id}, "
            f"trade_date={trade_date}: {exc}"
        ) from exc


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
    assert payload["matched"] is True
    assert payload["dsa_dir_bars"] == 60
    assert payload["offset_mean"] == 0.05
    print(f"_build_payload: {payload} ✓")

    print("OK")
