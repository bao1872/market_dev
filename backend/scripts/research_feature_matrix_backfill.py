"""研究特征矩阵回补脚本 - DB 主存储版。

从骨架升级为真实计算 + DB upsert：
1. 解析日期范围（--month 或 --start/--end 互斥）
2. 检查磁盘阈值（< 15GB 停止）
3. 获取 universe（symbols/limit/full）
4. 获取 trade_dates（从 bars_daily 查询）
5. 估算单月大小（> 3GB 停止）
6. dry-run → 打印计划并退出
7. 创建/resume run（status=running）
8. instrument-first：每只股票 load bars 1次 → compute_all_features → 按月份 trade_date 切片 → upsert
9. 检查失败率（> 5% 标 failed）
10. finalize run

与生产 stock_feature_snapshots 的区别：
- production snapshot: 服务最近交易日 + 自选股 + 前端展示，必须 point-in-time
- research matrix: 探索因子组合规律，可同时包含 causal/hindsight/label，但严格分命名空间
- 不接入 watchlist_ready，不修改生产 snapshot

用法：
    # dry-run 查看计划
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --month 2026-01 --dry-run

    # 2 symbols 验证
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --month 2026-01 --symbols 000001,600000

    # 100 stocks × 1 month
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --month 2026-01 --limit-instruments 100

    # 全市场 2026-01
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --month 2026-01

    # --resume 续跑（幂等 upsert，跳过已存在 instrument/date）
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --month 2026-01 --resume

    # 可选 debug 导出 parquet（不作为主存储）
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --month 2026-01 --symbols 000001 --export-parquet /tmp/debug.parquet

约束：
- DB 为主存储，parquet 只是可选 debug 导出
- 不写大 CSV/coverage/截图/大日志/DB 备份
- 不接入 watchlist_ready
- 不修改 production snapshot
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
import uuid
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.research_feature_matrix import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
)
from app.repositories.bar_repository import fetch_daily_bars
from app.research.feature_causality_registry import (
    NS_CAUSAL,
    NS_CONFIRMED_DELAY,
    NS_HINDSIGHT,
    NS_LABEL,
    build_default_registry,
)
from app.research.feature_computer import compute_all_features
from app.research.research_matrix_writer import (
    acquire_lock_file,
    acquire_run_lock,
    check_disk_threshold,
    check_failure_rate,
    check_month_size_threshold,
    create_or_resume_run,
    estimate_month_size,
    finalize_run,
    release_lock_file,
    resolve_month_range,
    upsert_rows_batch,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("research_feature_matrix_backfill")

# warmup：加载 start_date 前 400 天的 bars（约 16 个月，足够 250 日 BB/ATR + 120 日 percentile）
_WARMUP_DAYS = 400


def parse_args() -> argparse.Namespace:
    """解析 CLI 参数。

    --month 与 --start/--end 互斥。
    """
    parser = argparse.ArgumentParser(
        description="研究特征矩阵回补（DB 主存储，按月分批）",
    )
    # 日期范围（互斥组）
    date_group = parser.add_mutually_exclusive_group(required=True)
    date_group.add_argument(
        "--month",
        type=str,
        default=None,
        help="单月回补（YYYY-MM，如 2026-01）",
    )
    date_group.add_argument(
        "--start",
        type=str,
        default=None,
        help="起始日期（YYYY-MM-DD，与 --end 配合用于跨月 sample 验证）",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="latest",
        help="结束日期（YYYY-MM-DD 或 'latest'，默认 latest 表示最新 bars_daily 日期）",
    )
    # universe 过滤
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="只处理指定股票代码（逗号分隔，如 000001,600000）",
    )
    parser.add_argument(
        "--limit-instruments",
        type=int,
        default=None,
        help="限制处理的 instrument 数量（小样本验证）",
    )
    # 运行控制
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与估算，不写 DB，不写文件",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="续跑模式：已存在 run 则复用，已存在 instrument/date 幂等 upsert",
    )
    parser.add_argument(
        "--export-parquet",
        type=str,
        default=None,
        help="可选 debug 导出 parquet 路径（不作为主存储，仅 sample scope）",
    )
    args = parser.parse_args()

    # 解析 --symbols 为 list
    if args.symbols:
        args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    return args


def _resolve_scope(
    symbols: list[str] | None,
    limit_instruments: int | None,
) -> str:
    """根据过滤条件决定 scope 标签。

    - 有 --symbols → 'sample_symbols'
    - 有 --limit-instruments → 'sample_N'
    - 都无 → 'full'
    """
    if symbols and len(symbols) > 0:
        return "sample_symbols"
    if limit_instruments is not None and limit_instruments > 0:
        return f"sample_{limit_instruments}"
    return "full"


def _resolve_date_range(args: argparse.Namespace) -> tuple[date, date, str]:
    """解析日期范围与 month 标签。

    Returns:
        (start_date, end_date, month_label)
    """
    if args.month:
        start, end = resolve_month_range(args.month)
        return start, end, args.month

    # --start/--end 模式
    start = date.fromisoformat(args.start)
    if args.end == "latest":
        # latest 需要查 DB，先用占位符，后续替换
        end = date.today()
    else:
        end = date.fromisoformat(args.end)
    # month_label 用 start 的年月
    month_label = start.strftime("%Y-%m")
    return start, end, month_label


async def _get_instruments(
    db: AsyncSession,
    symbols: list[str] | None,
    limit: int | None,
) -> list[tuple[uuid.UUID, str]]:
    """获取待处理的 instrument 列表。

    Returns:
        [(instrument_id, symbol), ...]
    """
    stmt = select(Instrument.id, Instrument.symbol).where(
        Instrument.status == "active"
    )
    if symbols:
        stmt = stmt.where(Instrument.symbol.in_(symbols))
    else:
        # 默认只取 A 股（6 位数字 symbol）
        stmt = stmt.where(Instrument.symbol.op("~")(r"^\d{6}$"))
    if limit and limit > 0:
        stmt = stmt.limit(limit)
    result = await db.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def _get_trade_dates(
    db: AsyncSession,
    start: date,
    end: date,
) -> list[date]:
    """从 bars_daily 表查询已有交易日期。"""
    stmt = (
        select(func.distinct(BarDaily.trade_date))
        .where(BarDaily.trade_date >= start, BarDaily.trade_date <= end)
        .order_by(BarDaily.trade_date.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _get_latest_bar_date(db: AsyncSession) -> date | None:
    """查询 bars_daily 表中最新交易日。"""
    stmt = select(func.max(BarDaily.trade_date))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _build_row_dicts(
    run_id: uuid.UUID,
    instrument_id: uuid.UUID,
    symbol: str,
    features_df: pd.DataFrame,
    trade_dates: set[date],
) -> list[dict[str, Any]]:
    """将 features_df 转为 row dict 列表，只保留目标 trade_dates。

    Args:
        run_id: 所属 run ID
        instrument_id: 股票 ID
        symbol: 股票代码
        features_df: compute_all_features 返回的 DataFrame（index 为 trade_date）
        trade_dates: 目标月份的交易日集合

    Returns:
        row dict 列表，每个 dict 包含 run_id/instrument_id/symbol/trade_date + 33 个 feature 列
    """
    rows: list[dict[str, Any]] = []
    for trade_date, row in features_df.iterrows():
        # trade_date 可能是 pd.Timestamp 或 date
        td = trade_date.date() if hasattr(trade_date, "date") else trade_date
        if td not in trade_dates:
            continue
        row_dict: dict[str, Any] = {
            "run_id": run_id,
            "instrument_id": instrument_id,
            "symbol": symbol,
            "trade_date": td,
        }
        # 添加 33 个 feature 列（NaN → None）
        for col in features_df.columns:
            val = row[col]
            if pd.isna(val):
                row_dict[col] = None
            else:
                row_dict[col] = val.item() if hasattr(val, "item") else val
        rows.append(row_dict)
    return rows


async def _process_instrument(
    db: AsyncSession,
    run_id: uuid.UUID,
    instrument_id: uuid.UUID,
    symbol: str,
    trade_dates: set[date],
    start: date,
    end: date,
) -> tuple[int, int]:
    """处理单只 instrument：load bars → compute → upsert。

    [Blocker Fix] 失败率统计修正：
    - 单股失败时 failed_rows = len(trade_dates)（该股对应的所有交易日行）
    - 不再只计 1 行

    [Blocker Fix] DB 异常 rollback：
    - upsert 失败时 await db.rollback()，防止事务污染后续股票
    - rollback 后继续下一只

    Returns:
        (rows_written, rows_failed)
    """
    expected_rows = len(trade_dates)
    try:
        # load bars with warmup（start - 400 天 到 end）
        warmup_start = start - timedelta(days=_WARMUP_DAYS)
        bars = await fetch_daily_bars(db, instrument_id, warmup_start, end)
        if bars is None or bars.empty or len(bars) < 60:
            logger.warning(
                "bars 不足 instrument_id=%s symbol=%s len=%d",
                instrument_id, symbol, len(bars) if bars is not None else 0,
            )
            return 0, expected_rows

        # compute all features (per-bar full series)
        features_df = compute_all_features(bars)
        if features_df.empty:
            logger.warning("features 为空 symbol=%s", symbol)
            return 0, expected_rows

        # build row dicts (只保留目标 trade_dates)
        rows = _build_row_dicts(
            run_id, instrument_id, symbol, features_df, trade_dates
        )
        if not rows:
            logger.warning("无目标 trade_date 行 symbol=%s", symbol)
            return 0, expected_rows

        # upsert to DB
        count = await upsert_rows_batch(db, rows)
        return count, 0

    except Exception as exc:
        # [Blocker Fix] 异常时 rollback，防止同一 session 后续 upsert 因事务污染失败
        logger.error(
            "处理失败 instrument_id=%s symbol=%s: %s",
            instrument_id, symbol, exc,
        )
        try:
            await db.rollback()
        except Exception as rollback_exc:
            logger.error(
                "rollback 失败 instrument_id=%s symbol=%s: %s",
                instrument_id, symbol, rollback_exc,
            )
        return 0, expected_rows


async def _run_backfill(args: argparse.Namespace) -> None:
    """主回补流程。"""
    start_time = time.time()

    # 1. 解析日期范围
    start, end, month_label = _resolve_date_range(args)
    # 如果 --end=latest，查询最新 bar 日期
    if args.end == "latest" and not args.month:
        async with AsyncSessionLocal() as db:
            latest = await _get_latest_bar_date(db)
            if latest is None:
                print("[ERROR] bars_daily 表无数据，无法解析 --end latest")
                return
            end = latest

    print(f"[plan] 日期范围: {start} ~ {end} (month_label={month_label})")

    # 2. 检查磁盘阈值
    if not check_disk_threshold("/"):
        print("[BLOCKED] 磁盘剩余 < 15GB，停止")
        return
    print("[check] 磁盘空间 OK")

    # 3. 获取 universe + trade_dates
    async with AsyncSessionLocal() as db:
        instruments = await _get_instruments(db, args.symbols, args.limit_instruments)
        trade_dates = await _get_trade_dates(db, start, end)

    if not instruments:
        print("[ERROR] 无符合条件的 instrument")
        return
    if not trade_dates:
        print("[ERROR] 无符合条件的 trade_date")
        return

    scope = _resolve_scope(args.symbols, args.limit_instruments)
    instruments_count = len(instruments)
    trade_dates_count = len(trade_dates)

    # 4. 估算单月大小
    est_gb = estimate_month_size(instruments_count, trade_dates_count)
    expected_rows = instruments_count * trade_dates_count

    # 打印计划
    reg = build_default_registry()
    fc = {
        NS_CAUSAL: len(reg.by_namespace(NS_CAUSAL)),
        NS_CONFIRMED_DELAY: len(reg.by_namespace(NS_CONFIRMED_DELAY)),
        NS_HINDSIGHT: len(reg.by_namespace(NS_HINDSIGHT)),
        NS_LABEL: len(reg.by_namespace(NS_LABEL)),
    }
    print("=" * 60)
    print("[research_feature_matrix] 研究矩阵回补计划")
    print("=" * 60)
    print(f"month: {month_label}")
    print(f"date_range: {start} ~ {end}")
    print(f"scope: {scope}")
    if args.symbols:
        print(f"symbols: {args.symbols}")
    if args.limit_instruments:
        print(f"limit_instruments: {args.limit_instruments}")
    print(f"instruments_count: {instruments_count}")
    print(f"trade_dates_count: {trade_dates_count}")
    print(f"expected_rows: {expected_rows}")
    print(f"estimated_db_size: {est_gb:.4f} GB")
    print("-" * 60)
    print("字段分类统计:")
    print(f"  causal:           {fc[NS_CAUSAL]}")
    print(f"  confirmed_delay:  {fc[NS_CONFIRMED_DELAY]}")
    print(f"  hindsight:        {fc[NS_HINDSIGHT]}")
    print(f"  label:            {fc[NS_LABEL]}")
    print(f"  total_fields:     {sum(fc.values())}")
    print("=" * 60)

    # 5. 检查单月大小阈值
    if not check_month_size_threshold(est_gb):
        print(f"[BLOCKED] 单月预估 {est_gb:.2f}GB > 3GB，停止")
        return

    # 6. dry-run 退出
    if args.dry_run:
        print("[dry-run] 不写 DB，不写文件，只打印计划")
        return

    # [Blocker Fix] 7. 获取进程锁（pg_advisory_lock + lock file 双保险）
    #    防止同 month/scope 重复启动后台任务
    lock_file_path = acquire_lock_file(month_label, scope)
    if lock_file_path is None:
        print(
            f"[BLOCKED] lock file 已存在，同 month={month_label} scope={scope} "
            f"已有任务运行"
        )
        return

    # advisory lock 需要一个长生命周期 session（lock 跟随 session）
    lock_session = AsyncSessionLocal()
    acquired = await acquire_run_lock(lock_session, month=month_label, scope=scope)
    if not acquired:
        release_lock_file(lock_file_path)
        print(
            f"[BLOCKED] pg_advisory_lock 已被占用，同 month={month_label} "
            f"scope={scope} 已有任务运行"
        )
        return
    print(f"[lock] advisory_lock + lock_file 获取成功 path={lock_file_path}")

    try:
        # 8. 创建/resume run
        # [Blocker Fix] metadata 标记 Phase 1 字段范围 + DSA hindsight 未实现
        run_metadata = {
            "symbols": args.symbols,
            "limit": args.limit_instruments,
            "feature_version": "phase1_no_node_cluster",
            "dsa_hindsight_status": "not_implemented",
            "node_cluster_status": "not_implemented",
        }
        async with AsyncSessionLocal() as db:
            run = await create_or_resume_run(
                db,
                month=month_label,
                start_date=start,
                end_date=end,
                scope=scope,
                metadata=run_metadata,
            )
            await db.commit()
            print(f"[run] run_id={run.id} run_key={run.run_key} status={run.status}")

        # 8. instrument-first 回补
        # [Blocker Fix] 失败率统计：failed_rows 用 expected_rows 计算
        trade_dates_set = set(trade_dates)
        total_rows = 0
        total_failed_rows = 0
        total_failed_instruments = 0

        # 条件 import tqdm（可选依赖，缺失时无进度条）
        try:
            from tqdm import tqdm as _tqdm_iter
        except ImportError:
            _tqdm_iter = None  # type: ignore[assignment]

        instruments_iter: Iterable[tuple[uuid.UUID, str]] = instruments
        if _tqdm_iter is not None:
            instruments_iter = _tqdm_iter(instruments, desc="instruments", unit="stock")

        # 每 100 只 instrument commit 一次
        commit_batch = 100
        processed = 0

        async with AsyncSessionLocal() as db:
            for instrument_id, symbol in instruments_iter:
                rows, failed = await _process_instrument(
                    db, run.id, instrument_id, symbol, trade_dates_set, start, end
                )
                total_rows += rows
                total_failed_rows += failed
                if failed > 0:
                    total_failed_instruments += 1
                processed += 1

                # 每 commit_batch 只 commit 一次
                if processed % commit_batch == 0:
                    await db.commit()
                    logger.info(
                        "checkpoint: processed=%d rows=%d failed_rows=%d "
                        "failed_instruments=%d",
                        processed, total_rows, total_failed_rows,
                        total_failed_instruments,
                    )

            # 最终 commit
            await db.commit()

        # 9. 检查失败率（[Blocker Fix] 用 failed_rows / expected_rows）
        duration = time.time() - start_time
        final_status = STATUS_SUCCEEDED
        if not check_failure_rate(total_failed_rows, expected_rows):
            print(
                f"[BLOCKED] 失败率 {total_failed_rows}/{expected_rows} = "
                f"{total_failed_rows/max(expected_rows,1)*100:.1f}% > 5%，标 failed"
            )
            final_status = STATUS_FAILED

        # 10. finalize run
        from app.models.research_feature_matrix import ResearchFeatureMatrixRun

        async with AsyncSessionLocal() as db:
            # 重新加载 run（跨 session）
            stmt = select(ResearchFeatureMatrixRun).where(
                ResearchFeatureMatrixRun.id == run.id
            )
            result = await db.execute(stmt)
            run_fresh = result.scalar_one()
            # [Blocker Fix] metadata 合并 Phase 1 标记 + failed_instruments/failed_rows
            final_metadata = dict(run_metadata)
            final_metadata["phase"] = "phase1"
            await finalize_run(
                db,
                run_fresh,
                status=final_status,
                instruments_count=instruments_count,
                trade_dates_count=trade_dates_count,
                rows_count=total_rows,
                failed_count=total_failed_rows,
                duration_seconds=duration,
                metadata=final_metadata,
                failed_instruments=total_failed_instruments,
            )
            await db.commit()

        print("=" * 60)
        print(f"[done] status={final_status}")
        print(f"  instruments: {instruments_count}")
        print(f"  trade_dates: {trade_dates_count}")
        print(f"  rows_written: {total_rows}")
        print(f"  rows_failed: {total_failed_rows}")
        print(f"  failed_instruments: {total_failed_instruments}")
        print(f"  duration: {duration:.1f}s")
        print(f"  run_id: {run.id}")
        print("=" * 60)

        # 11. 可选 debug 导出 parquet
        if args.export_parquet:
            # 校验 sample scope
            if scope == "full":
                print("[WARN] --export-parquet 在 full scope 下跳过（禁止全市场导出文件）")
            else:
                await _export_parquet(args.export_parquet, run.id)

    finally:
        # [Blocker Fix] 释放锁：advisory lock 随 session 关闭自动释放，
        # lock file 需手动删除
        try:
            await lock_session.close()
        except Exception as exc:
            logger.error("关闭 lock_session 失败: %s", exc)
        release_lock_file(lock_file_path)
        print(f"[lock] advisory_lock + lock_file 已释放 path={lock_file_path}")


async def _export_parquet(path: str, run_id: uuid.UUID) -> None:
    """可选 debug 导出 parquet（不作为主存储）。"""
    from app.models.research_feature_matrix import ResearchFeatureMatrixRow

    async with AsyncSessionLocal() as db:
        stmt = select(ResearchFeatureMatrixRow).where(
            ResearchFeatureMatrixRow.run_id == run_id
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()

    if not rows:
        print(f"[export] 无数据可导出 run_id={run_id}")
        return

    # 转为 DataFrame
    data = []
    for r in rows:
        row_dict = {c.name: getattr(r, c.name) for c in ResearchFeatureMatrixRow.__table__.columns}  # type: ignore[attr-defined]
        data.append(row_dict)
    df = pd.DataFrame(data)
    df.to_parquet(path, index=False)
    print(f"[export] 导出 {len(df)} 行到 {path}")


def main() -> None:
    """主入口。"""
    args = parse_args()
    asyncio.run(_run_backfill(args))


if __name__ == "__main__":
    main()
