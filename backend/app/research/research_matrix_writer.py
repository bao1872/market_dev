"""research_matrix_writer - 研究特征矩阵 DB 写入与运行生命周期管理。

提供：
1. 三道硬阈值：磁盘剩余 < 15GB / 单月预估 > 3GB / 失败率 > 5%
2. monthly run 生命周期：create_or_resume_run / finalize_run
3. 批量 upsert 到 research_feature_matrix_rows（ON CONFLICT DO UPDATE 幂等覆盖）
4. 月份 → (start_date, end_date) 解析
5. 单月 DB 占用估算（estimated_db_size）

设计：
- 与生产 stock_feature_snapshots 严格分离，不接入 watchlist_ready
- run_key = f"{month}_{scope}"，支持 --resume 幂等
- rows 表唯一键 (instrument_id, trade_date) 跨 run 幂等覆盖
- 批量 upsert 单批上限 1000 行（asyncpg 参数限制保守值）

用法：
    from app.research.research_matrix_writer import (
        create_or_resume_run, upsert_rows_batch, finalize_run,
        check_disk_threshold, resolve_month_range,
    )

模块自测：
    python -m app.research.research_matrix_writer
"""

from __future__ import annotations

import calendar
import shutil
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_feature_matrix import (
    STATUS_RUNNING,
    ResearchFeatureMatrixRow,
    ResearchFeatureMatrixRun,
)

# =============================================================================
# 硬阈值常量
# =============================================================================

# 磁盘剩余空间下限（GB）：低于此值停止
DISK_MIN_GB = 15

# 单月 DB 占用上限（GB）：超过此值停止
MONTH_SIZE_MAX_GB = 3.0

# 失败率上限（0-1）：超过此值停止
FAILURE_RATE_MAX = 0.05

# 批量 upsert 单批行数上限（asyncpg 参数 32767，每行 39 列 → 保守 1000）
UPSERT_BATCH_SIZE = 1000

# 单行 DB 占用估算（39 列 × 平均 ~50 字节 ≈ 2KB）
_BYTES_PER_ROW = 2048

# 不参与 ON CONFLICT DO UPDATE 的列（主键 + 冲突键 + created_at）
_NO_UPDATE_COLS = frozenset({"id", "instrument_id", "trade_date", "created_at"})


# =============================================================================
# 1. 硬阈值检查（纯函数）
# =============================================================================


def check_disk_threshold(path: str = "/") -> bool:
    """检查磁盘剩余空间是否 >= DISK_MIN_GB。

    Args:
        path: 检查路径（默认根分区）

    Returns:
        True if free >= DISK_MIN_GB，False 则应停止
    """
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    return free_gb >= DISK_MIN_GB


def check_month_size_threshold(estimated_gb: float) -> bool:
    """检查单月预估 DB 占用是否 <= MONTH_SIZE_MAX_GB。

    Args:
        estimated_gb: estimate_month_size() 返回值

    Returns:
        True if <= MONTH_SIZE_MAX_GB，False 则应停止
    """
    return estimated_gb <= MONTH_SIZE_MAX_GB


def check_failure_rate(failed: int, total: int) -> bool:
    """检查失败率是否 <= FAILURE_RATE_MAX。

    Args:
        failed: 失败行数
        total: 总行数

    Returns:
        True if rate <= FAILURE_RATE_MAX 或 total=0，False 则应停止
    """
    if total == 0:
        return True
    return (failed / total) <= FAILURE_RATE_MAX


# =============================================================================
# 2. 月份解析与大小估算（纯函数）
# =============================================================================


def resolve_month_range(month: str) -> tuple[date, date]:
    """将 YYYY-MM 字符串解析为 (start_date, end_date)。

    Args:
        month: YYYY-MM 格式（如 "2026-01"）

    Returns:
        (month_first_day, month_last_day)

    Raises:
        ValueError: 格式非法或月份越界
    """
    parts = month.split("-")
    if len(parts) != 2:
        raise ValueError(f"month 格式应为 YYYY-MM，当前={month!r}")
    try:
        year = int(parts[0])
        m = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"month 格式应为 YYYY-MM，当前={month!r}") from exc
    if m < 1 or m > 12:
        raise ValueError(f"月份越界: {m}（应在 1-12）")

    last_day = calendar.monthrange(year, m)[1]
    return date(year, m, 1), date(year, m, last_day)


def estimate_month_size(instruments_count: int, trade_dates_count: int) -> float:
    """估算单月 DB 占用（GB）。

    估算公式：rows × _BYTES_PER_ROW / (1024^3)

    Args:
        instruments_count: 股票数
        trade_dates_count: 交易日数

    Returns:
        预估 DB 占用 GB
    """
    rows = instruments_count * trade_dates_count
    bytes_total = rows * _BYTES_PER_ROW
    return bytes_total / (1024**3)


# =============================================================================
# 3. monthly run 生命周期（DB）
# =============================================================================


async def create_or_resume_run(
    db: AsyncSession,
    *,
    month: str,
    start_date: date,
    end_date: date,
    scope: str,
    metadata: dict[str, Any] | None = None,
) -> ResearchFeatureMatrixRun:
    """创建或恢复 monthly run。

    run_key = f"{month}_{scope}"，相同 run_key 返回已存在 run（支持 --resume）。
    新建 run 的 status=running，started_at=now。

    Args:
        db: 异步会话
        month: YYYY-MM
        start_date: 起始日期
        end_date: 结束日期
        scope: 'full' / 'sample_N'
        metadata: 可选小摘要（不存完整 payload）

    Returns:
        ResearchFeatureMatrixRun（status=running 或已存在状态）
    """
    run_key = f"{month}_{scope}"
    stmt = select(ResearchFeatureMatrixRun).where(
        ResearchFeatureMatrixRun.run_key == run_key
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing is not None:
        return existing

    run = ResearchFeatureMatrixRun(
        run_key=run_key,
        month=month,
        start_date=start_date,
        end_date=end_date,
        status=STATUS_RUNNING,
        started_at=datetime.now(UTC),
        metadata_json=metadata,
    )
    db.add(run)
    await db.flush()
    return run


async def finalize_run(
    db: AsyncSession,
    run: ResearchFeatureMatrixRun,
    *,
    status: str,
    instruments_count: int,
    trade_dates_count: int,
    rows_count: int,
    failed_count: int,
    duration_seconds: float,
    metadata: dict[str, Any] | None = None,
) -> None:
    """终结 run：更新 status/统计/duration/finished_at。

    Args:
        db: 异步会话
        run: 待终结的 run
        status: STATUS_SUCCEEDED / STATUS_FAILED
        instruments_count: 实际处理的 instrument 数
        trade_dates_count: 实际处理的交易日数
        rows_count: 成功写入行数
        failed_count: 失败行数
        duration_seconds: 总耗时秒
        metadata: 可选附加 metadata（合并到现有）
    """
    run.status = status
    run.instruments_count = instruments_count
    run.trade_dates_count = trade_dates_count
    run.rows_count = rows_count
    run.failed_count = failed_count
    run.duration_seconds = duration_seconds
    run.finished_at = datetime.now(UTC)
    if metadata is not None:
        run.metadata_json = metadata
    await db.flush()


# =============================================================================
# 4. 批量 upsert rows（DB）
# =============================================================================


async def upsert_rows_batch(
    db: AsyncSession,
    rows: list[dict[str, Any]],
) -> int:
    """批量 upsert 到 research_feature_matrix_rows。

    使用 PostgreSQL INSERT ... ON CONFLICT (instrument_id, trade_date) DO UPDATE，
    相同 (instrument_id, trade_date) 的行被覆盖（幂等）。
    自动分批，每批 UPSERT_BATCH_SIZE 行。

    Args:
        db: 异步会话
        rows: 行 dict 列表，每个 dict 必须包含 run_id / instrument_id /
              symbol / trade_date + 可选的 feature 列

    Returns:
        实际写入行数（= len(rows)，幂等 upsert 不区分 insert/update）

    Raises:
        Exception: DB 写入失败时 re-raise（不吞异常）
    """
    if not rows:
        return 0

    # 预计算 ON CONFLICT DO UPDATE 的 set_ 字典（排除主键/冲突键/created_at）
    update_cols = {
        col.name: pg_insert(ResearchFeatureMatrixRow).excluded[col.name]
        for col in ResearchFeatureMatrixRow.__table__.columns  # type: ignore[attr-defined]
        if col.name not in _NO_UPDATE_COLS
    }

    total = 0
    for i in range(0, len(rows), UPSERT_BATCH_SIZE):
        batch = rows[i : i + UPSERT_BATCH_SIZE]
        stmt = (
            pg_insert(ResearchFeatureMatrixRow)
            .values(batch)
            .on_conflict_do_update(
                index_elements=["instrument_id", "trade_date"],
                set_=update_cols,
            )
        )
        await db.execute(stmt)
        total += len(batch)
    await db.flush()
    return total


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯函数自测（不连接数据库）
    # 1. 月份解析
    assert resolve_month_range("2026-01") == (date(2026, 1, 1), date(2026, 1, 31))
    assert resolve_month_range("2026-02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert resolve_month_range("2024-02") == (date(2024, 2, 1), date(2024, 2, 29))  # 闰年
    assert resolve_month_range("2026-12") == (date(2026, 12, 1), date(2026, 12, 31))
    print("resolve_month_range ✓")

    # 2. 失败率阈值
    assert check_failure_rate(failed=6, total=100) is False  # 6% > 5%
    assert check_failure_rate(failed=5, total=100) is True  # 5% = 5%（边界）
    assert check_failure_rate(failed=3, total=100) is True  # 3% < 5%
    assert check_failure_rate(failed=0, total=0) is True  # 无数据
    print("check_failure_rate ✓")

    # 3. 月份大小阈值
    assert check_month_size_threshold(3.5) is False
    assert check_month_size_threshold(2.0) is True
    assert check_month_size_threshold(float(MONTH_SIZE_MAX_GB)) is True  # 边界
    print("check_month_size_threshold ✓")

    # 4. 月份大小估算
    est = estimate_month_size(instruments_count=5000, trade_dates_count=20)
    assert 0.1 < est < 1.0, f"估算异常: {est}"
    assert estimate_month_size(0, 20) == 0.0
    print(f"estimate_month_size(5000, 20) = {est:.4f} GB ✓")

    # 5. 磁盘阈值（真实检查）
    disk_ok = check_disk_threshold("/")
    print(f"check_disk_threshold('/') = {disk_ok}")

    print("OK")
