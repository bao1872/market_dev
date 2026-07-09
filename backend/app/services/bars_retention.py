"""行情数据保留策略 - 清理过期数据。

保守保留策略（用户确认）：
- 日线/周线/月线：永久保留（不清理）
- 15min/60min：保留 2 年（730 天）
- 1min：保留 30 天（预留配置，当前 1m 不参与定时刷新，仅在指标计算时按需查询）

清理方式：
- 使用 DELETE FROM ... WHERE trade_time < :cutoff（向量化删除，非逐行）
- 支持 dry_run 预检模式（只统计不删除）
- 清理后记录 Prometheus 指标（bars_retention_deleted_total）

调度：
- 当前未配置自动调度，需手动调用或后续添加定时任务
- 在 bars_scheduler_service.py 的 run_retention_cleanup 方法中调用

Inputs:
    session: AsyncSession
    dry_run: True 时只统计不删除

Outputs:
    list[RetentionResult]: 各表的清理结果

How to Run:
    python -m app.services.bars_retention    # 自测：验证保留策略逻辑
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute
from app.services.bars_metrics import bars_retention_deleted_total

logger = logging.getLogger("bars_retention")

# 保留期限（天）
_RETENTION_15MIN_DAYS = 730  # 2 年
_RETENTION_60MIN_DAYS = 730  # 2 年
_RETENTION_MINUTE_DAYS = 30  # 30 天（1m 数据量大，严格控制保留期限）


@dataclass
class RetentionResult:
    """保留策略执行结果。

    Attributes:
        table_name: 表名
        deleted_count: 删除记录数（dry_run 模式下为待删除数）
        cutoff_date: 删除截止日期（早于此日期的数据被删除/统计）；永久保留时为 None
    """

    table_name: str
    deleted_count: int
    cutoff_date: date | datetime | None


# 保留策略配置：(模型类, 时间字段名, 保留天数, 是否永久保留)
# 永久保留的表（daily）不参与清理；周线/月线不存储在 DB，从日线动态合成
_RETENTION_CONFIG: list[
    tuple[
        type[BarDaily] | type[Bar15Min] | type[Bar60Min] | type[BarMinute],
        str,
        int | None,
        bool,
    ]
] = [
    (BarDaily, "trade_date", None, True),      # 永久保留
    (Bar15Min, "trade_time", _RETENTION_15MIN_DAYS, False),  # 保留 2 年
    (Bar60Min, "trade_time", _RETENTION_60MIN_DAYS, False),  # 保留 2 年
    (BarMinute, "trade_time", _RETENTION_MINUTE_DAYS, False),  # 保留 30 天（预留配置，当前 1m 不参与定时刷新）
]


async def apply_retention_policy(
    session: AsyncSession,
    dry_run: bool = False,
) -> list[RetentionResult]:
    """执行数据保留策略，清理过期数据。

    Args:
        session: 异步会话
        dry_run: True 时只统计不删除（用于预检）

    Returns:
        各表的清理结果列表（永久保留的表返回 deleted_count=0, cutoff_date=None）

    Raises:
        Exception: 删除失败时 re-raise（不吞没）
    """
    results: list[RetentionResult] = []
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    for model_cls, time_col, retention_days, is_permanent in _RETENTION_CONFIG:
        table_name = model_cls.__tablename__

        # 永久保留的表：跳过清理
        if is_permanent or retention_days is None:
            results.append(RetentionResult(
                table_name=table_name,
                deleted_count=0,
                cutoff_date=None,
            ))
            continue

        # 计算 cutoff 日期
        cutoff = now - timedelta(days=retention_days)
        time_column = getattr(model_cls, time_col)

        try:
            if dry_run:
                # 预检模式：只统计待删除行数
                count_stmt = select(func.count()).select_from(model_cls).where(
                    time_column < cutoff
                )
                count_result = await session.execute(count_stmt)
                deleted_count = int(count_result.scalar() or 0)
                logger.info(
                    "保留策略预检 %s cutoff=%s 待删除=%d（dry_run）",
                    table_name, cutoff.isoformat(), deleted_count,
                )
            else:
                # 实际删除模式：先统计再删除
                count_stmt = select(func.count()).select_from(model_cls).where(
                    time_column < cutoff
                )
                count_result = await session.execute(count_stmt)
                deleted_count = int(count_result.scalar() or 0)

                if deleted_count > 0:
                    # 执行删除（向量化 DELETE）
                    delete_stmt = text(
                        f"DELETE FROM {table_name} WHERE {time_col} < :cutoff"
                    )
                    await session.execute(delete_stmt, {"cutoff": cutoff})
                    await session.commit()

                    # 记录 Prometheus 指标
                    bars_retention_deleted_total.labels(
                        table_name=table_name
                    ).inc(deleted_count)

                    logger.info(
                        "保留策略清理 %s cutoff=%s deleted=%d",
                        table_name, cutoff.isoformat(), deleted_count,
                    )
                else:
                    logger.debug(
                        "保留策略清理 %s 无过期数据 cutoff=%s",
                        table_name, cutoff.isoformat(),
                    )
        except Exception as exc:
            logger.error(
                "保留策略执行失败 %s cutoff=%s: %s",
                table_name, cutoff.isoformat(), exc,
            )
            await session.rollback()
            raise

        results.append(RetentionResult(
            table_name=table_name,
            deleted_count=deleted_count,
            cutoff_date=cutoff,
        ))

    return results


def get_retention_config() -> list[dict]:
    """获取保留策略配置（供文档生成与监控使用）。

    Returns:
        各表的保留策略配置列表
    """
    config_list = []
    for model_cls, time_col, retention_days, is_permanent in _RETENTION_CONFIG:
        config_list.append({
            "table_name": model_cls.__tablename__,
            "time_column": time_col,
            "retention_days": retention_days,
            "is_permanent": is_permanent,
            "retention_desc": "永久保留" if is_permanent else f"{retention_days} 天",
        })
    return config_list


if __name__ == "__main__":
    # 自测入口：验证保留策略配置与逻辑（无副作用，不连 DB）
    print("===== Phase 5.2 bars_retention 自测 =====")

    # 1. 验证保留策略配置
    config = get_retention_config()
    assert len(config) == 4, f"应有 4 张表配置，实际 {len(config)}"
    print(f"保留策略配置（{len(config)} 张表）:")

    permanent_count = 0
    for c in config:
        print(f"  - {c['table_name']}: {c['retention_desc']} (时间列: {c['time_column']})")
        if c["is_permanent"]:
            permanent_count += 1

    assert permanent_count == 1, f"应有 1 张永久保留表，实际 {permanent_count}"
    print(f"✓ 永久保留表数量: {permanent_count}（daily）")

    # 2. 验证配置值
    config_by_table = {c["table_name"]: c for c in config}
    assert config_by_table["bars_daily"]["is_permanent"] is True
    assert config_by_table["bars_15min"]["retention_days"] == 730
    assert config_by_table["bars_60min"]["retention_days"] == 730
    assert config_by_table["bars_minute"]["retention_days"] == 30
    print("✓ 保留期限配置正确（15min/60min=730天, minute=30天）")

    # 3. 验证 cutoff 计算逻辑
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff_15min = now - timedelta(days=730)
    cutoff_minute = now - timedelta(days=30)
    print(f"✓ 15min cutoff 计算: {cutoff_15min.isoformat()}")
    print(f"✓ minute cutoff 计算: {cutoff_minute.isoformat()}")

    # 4. 验证 RetentionResult 数据类
    result_permanent = RetentionResult(
        table_name="bars_daily",
        deleted_count=0,
        cutoff_date=None,
    )
    result_cleaned = RetentionResult(
        table_name="bars_15min",
        deleted_count=1000,
        cutoff_date=cutoff_15min,
    )
    assert result_permanent.cutoff_date is None
    assert result_permanent.deleted_count == 0
    assert result_cleaned.deleted_count == 1000
    assert result_cleaned.cutoff_date == cutoff_15min
    print("✓ RetentionResult 数据类验证通过")

    # 5. 验证 apply_retention_policy 函数签名
    import inspect
    sig = inspect.signature(apply_retention_policy)
    params = list(sig.parameters.keys())
    assert params == ["session", "dry_run"], f"参数应为 [session, dry_run]，实际 {params}"
    assert sig.parameters["dry_run"].default is False, "dry_run 默认应为 False"
    print("✓ apply_retention_policy 签名验证通过")

    print("\n所有自测通过 ✓（未进行 DB 测试）")
