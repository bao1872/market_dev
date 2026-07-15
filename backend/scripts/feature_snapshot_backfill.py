"""特征快照历史回补脚本 - instrument-first 优化版。

用法：
    # 单进程模式（默认）
    cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \\
        --start 2026-01-01 --end latest --batch-size 20

    # 多进程模式（--workers > 1）
    cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \\
        --start 2026-01-01 --end latest --workers 4 --resume

参数：
    --start: 起始日期（YYYY-MM-DD，必填）
    --end: 结束日期（YYYY-MM-DD 或 "latest"，默认 "latest" 表示最新 bars_daily 日期）
    --batch-size: 每批 instrument 数（默认 20，保守内存；仅单进程模式生效）
    --symbols: 只处理指定股票代码（逗号分隔，如 000100,603303）
    --limit-instruments: 限制处理的 instrument 数量（用于小样本验证）
    --resume: 跳过已存在 snapshot 且所属 trade_date 的 run.status='succeeded' 的行
    --dry-run: 只打印计划与 missing 统计，不执行写入
    --failure-threshold: 失败比例阈值（默认 0.3，超过则该 trade_date 标 run.status='failed'）
    --workers: 并行进程数（默认 1 单进程，>1 启用 multiprocessing；建议 = CPU 核数）

[instrument-first 优化]：
    旧 date-first 实现已废弃（每只 instrument 每个 trade_date 重复 fetch bars）。
    新 instrument-first：每只 instrument 只加载一次 1d/15m bars，遍历 trade_dates 在内存切片。

    for instrument_batch in active instruments:
        一次性加载该 batch 每只股票的 1d/15m bars
        for trade_date in trade_dates:
            compute_feature_snapshot_for_date(primary_bars=..., secondary_bars=...)
            upsert

[multiprocessing 优化]（--workers > 1）：
    - 主进程创建 run records + 分发 instrument chunks
    - 每个 worker 独立 async engine + session
    - per-date commit（被 kill 不丢已完成，resume 可续）
    - 单 worker 失败不阻塞其他
    - 预期 4-8x 提速（取决于 CPU 核数）

[事务边界 + run gate]：
    - 单进程：backfill_instrument_first 不内部 commit，由 main 控制
    - 多进程：backfill_instrument_first_parallel 创建 run records 后 commit，
      每个 worker per-date commit，主进程 finalize run records
    - 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError）
    - 单股失败不阻塞其他股票
    - watchlist 只读取 run.status='succeeded' + published_at IS NOT NULL + metadata_.scope='full' 的 snapshot

约束：
    - 不修改 DSA/BB/swing/temporal 数学公式
    - 复用 feature_snapshot_service.compute_feature_snapshot_for_date
    - 单股失败记录到日志，不阻塞其他股票
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
import warnings
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.feature_snapshot_service import (
    PublishedSnapshotRunExistsError,
    _fetch_bars_from_db,
    compute_feature_snapshot_for_date,
    create_snapshot_run,
    finish_snapshot_run,
    upsert_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("feature_snapshot_backfill")


# 默认参数（与 feature_snapshot_service 对齐）
_DEFAULT_PRIMARY_TF = "1d"
_DEFAULT_SECONDARY_TF = "15m"
_DEFAULT_ADJ = "qfq"
_DEFAULT_SCHEMA_VERSION = 1

# [Blocker Fix] - run scope 枚举
SCOPE_FULL = "full"  # 全市场 backfill / after_close，watchlist 可读
SCOPE_SAMPLE = "sample"  # --symbols / --limit-instruments 小样本，watchlist 不可读


def _resolve_run_scope(
    symbols: list[str] | None,
    limit_instruments: int | None,
) -> str:
    """根据 --symbols / --limit-instruments 决定 run scope。

    - 任一过滤条件启用 → 'sample'（小样本验证，不发布到 watchlist）
    - 都未启用 → 'full'（全市场，发布到 watchlist）

    Args:
        symbols: --symbols 参数（已解析为 list[str]），None 表示未设置
        limit_instruments: --limit-instruments 参数，None 表示未设置

    Returns:
        'sample' 或 'full'
    """
    if symbols and len(symbols) > 0:
        return SCOPE_SAMPLE
    if limit_instruments is not None and limit_instruments > 0:
        return SCOPE_SAMPLE
    return SCOPE_FULL


class ProfileCollector:
    """轻量性能诊断收集器：记录各步骤耗时并输出聚合统计。

    设计原则：
    - 不写文件、不输出逐 instrument 明细
    - 只在 stdout 输出聚合统计（total/avg/p50/p95）
    - 支持多进程 merge（worker 返回 ProfileCollector，主进程合并）
    - 默认不启用，只有 --profile-summary 才实例化
    """

    def __init__(self) -> None:
        # {step_name: [ms_sample1, ms_sample2, ...]}
        self._samples: dict[str, list[float]] = {}

    def record(self, step: str, ms: float) -> None:
        """记录单个步骤的单次耗时（毫秒）。"""
        self._samples.setdefault(step, []).append(ms)

    def merge(self, other: ProfileCollector) -> None:
        """合并另一个 ProfileCollector 的样本（多进程模式）。"""
        for step, samples in other._samples.items():
            self._samples.setdefault(step, []).extend(samples)

    def compute_stats(self) -> dict[str, dict[str, float]]:
        """计算每个步骤的聚合统计：count/total/avg/p50/p95。"""
        import statistics

        stats: dict[str, dict[str, float]] = {}
        for step, samples in self._samples.items():
            if not samples:
                continue
            sorted_samples = sorted(samples)
            n = len(sorted_samples)
            total = sum(sorted_samples)
            # p50 = median
            p50 = statistics.median(sorted_samples)
            # p95 = 95th percentile（线性插值）
            if n == 1:
                p95 = sorted_samples[0]
            else:
                idx = 0.95 * (n - 1)
                lo = int(idx)
                hi = min(lo + 1, n - 1)
                frac = idx - lo
                p95 = sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac
            stats[step] = {
                "count": n,
                "total": total,
                "avg": total / n,
                "p50": p50,
                "p95": p95,
            }
        return stats

    def format_summary(
        self,
        instruments_total: int,
        trade_dates_total: int,
        rows_new: int,
        rows_skipped: int,
        rows_failed: int,
        worker_count: int,
    ) -> str:
        """格式化聚合统计为可读字符串（stdout 输出）。"""
        stats = self.compute_stats()
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("[PROFILE-SUMMARY] 聚合性能统计")
        lines.append("=" * 60)
        lines.append(f"instruments_total: {instruments_total}")
        lines.append(f"trade_dates_total: {trade_dates_total}")
        lines.append(f"rows_new: {rows_new}")
        lines.append(f"rows_skipped: {rows_skipped}")
        lines.append(f"rows_failed: {rows_failed}")
        lines.append(f"worker_count: {worker_count}")
        lines.append("-" * 60)
        for step in sorted(stats.keys()):
            s = stats[step]
            lines.append(
                f"{step}: total={s['total']:.1f}ms avg={s['avg']:.1f}ms "
                f"p50={s['p50']:.1f}ms p95={s['p95']:.1f}ms count={s['count']}"
            )
        # estimated_full_day_time：基于 avg per instrument × 全市场 instruments × trade_dates
        total_per_inst = stats.get("total_ms_per_instrument")
        if total_per_inst and instruments_total > 0 and trade_dates_total > 0:
            est = total_per_inst["avg"] * instruments_total * trade_dates_total / 1000
            lines.append(f"estimated_full_day_time: {est:.1f}s")
        lines.append("=" * 60)
        return "\n".join(lines)


async def get_trade_dates_from_bars(
    db: AsyncSession,
    start: date,
    end: date,
) -> list[date]:
    """从 bars_daily 表查询已有交易日期（确保数据存在再计算 snapshot）。

    Args:
        db: 异步会话
        start: 起始日期
        end: 结束日期

    Returns:
        已有 bars_daily 数据的交易日期列表（升序）
    """
    stmt = (
        select(func.distinct(BarDaily.trade_date))
        .where(BarDaily.trade_date >= start, BarDaily.trade_date <= end)
        .order_by(BarDaily.trade_date.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_latest_bar_date(db: AsyncSession) -> date | None:
    """查询 bars_daily 表中最新交易日。"""
    stmt = select(func.max(BarDaily.trade_date))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_existing_instrument_ids(
    db: AsyncSession,
    trade_date: date,
    *,
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
) -> set[uuid.UUID]:
    """查询某交易日已存在 snapshot 的 instrument_id 集合。

    按完整唯一键 (instrument_id, trade_date, primary_timeframe, secondary_timeframe,
    adj, schema_version) 过滤。

    Args:
        db: 异步会话
        trade_date: 业务交易日
        primary_timeframe: 主时间周期
        secondary_timeframe: 次时间周期
        adj: 复权方式
        schema_version: 快照 schema 版本

    Returns:
        已存在 snapshot 的 instrument_id 集合
    """
    stmt = select(StockFeatureSnapshot.instrument_id).where(
        StockFeatureSnapshot.trade_date == trade_date,
        StockFeatureSnapshot.primary_timeframe == primary_timeframe,
        StockFeatureSnapshot.secondary_timeframe == secondary_timeframe,
        StockFeatureSnapshot.adj == adj,
        StockFeatureSnapshot.schema_version == schema_version,
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def get_instruments_for_backfill(
    db: AsyncSession,
    *,
    symbols: list[str] | None = None,
    limit: int | None = None,
) -> list[uuid.UUID]:
    """获取待回补的 instrument 列表（支持 --symbols / --limit-instruments 过滤）。

    Args:
        db: 异步会话
        symbols: 股票代码过滤（如 ['000100', '603303']）；None 表示不过滤
        limit: 限制数量（如 20）；None 表示不限制

    Returns:
        instrument_id 列表
    """
    stmt = select(Instrument.id).where(Instrument.status == "active")

    if symbols is not None and len(symbols) > 0:
        # --symbols 优先于 6 位数字过滤
        stmt = stmt.where(Instrument.symbol.in_(symbols))
    else:
        # 默认只取 A 股（6 位数字 symbol），与 get_active_a_share_instruments 一致
        stmt = stmt.where(Instrument.symbol.op("~")(r"^\d{6}$"))

    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def load_instrument_bars(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """加载单个 instrument 的全部 1d + 15m bars（一次性，供后续内存切片复用）。

    通过复用 feature_snapshot_service._fetch_bars_from_db 实现：
    - 该函数返回 instrument 全部历史 bars（不按 trade_date 过滤）
    - backfill_instrument_first 在内存按 trade_date 切片

    Args:
        db: 异步会话
        instrument_id: 标的 UUID
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 次周期（默认 15m）
        adj: 复权方式（默认 qfq）

    Returns:
        (primary_bars, secondary_bars) 元组；失败时对应位置为 None
    """
    # trade_date 参数对 _fetch_bars_from_db 无实际过滤作用（返回全量历史）
    # 传 date.max 仅作占位符，未来若 _fetch_bars_from_db 改为按日期过滤时需调整
    placeholder_date = date.max
    primary_bars = await _fetch_bars_from_db(
        db, instrument_id, primary_timeframe, adj, placeholder_date,
    )
    secondary_bars = await _fetch_bars_from_db(
        db, instrument_id, secondary_timeframe, adj, placeholder_date,
    )
    return primary_bars, secondary_bars


async def _get_succeeded_trade_dates(
    db: AsyncSession,
    trade_dates: list[date],
    *,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
) -> set[date]:
    """查询哪些 trade_date 已有 succeeded run（用于 --resume 判断）。

    --resume 语义：
    - 跳过 已存在 snapshot 且 所属 trade_date 的 run.status='succeeded' 的行
    - 如果 run.status='failed'，不跳过（允许重试）

    Args:
        db: 异步会话
        trade_dates: 待查询的交易日列表
        schema_version: 快照 schema 版本

    Returns:
        已 succeeded 的 trade_date 集合
    """
    if not trade_dates:
        return set()
    stmt = (
        select(StockFeatureSnapshotRun.trade_date)
        .where(
            StockFeatureSnapshotRun.trade_date.in_(trade_dates),
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
        )
        .distinct()
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def backfill_instrument_first(
    db: AsyncSession,
    trade_dates: list[date],
    instruments: list[uuid.UUID],
    *,
    batch_size: int = 20,
    failure_threshold: float = 0.3,
    resume: bool = False,
    dry_run: bool = False,
    run_type: str = "backfill",
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
    scope: str = SCOPE_FULL,
    profile: ProfileCollector | None = None,
) -> dict[str, Any]:
    """Instrument-first 回补：每只 instrument 加载 bars 一次，遍历 trade_dates 切片。

    [instrument-first 优化]
    - 外层循环按 instrument batch
    - 每只 instrument 只加载 1 次 1d/15m bars
    - 内层循环按 trade_date，在内存切片（compute_feature_snapshot_for_date 内部 _truncate_bars_to_trade_date）

    [事务边界 + run gate]
    - 本函数不内部 commit，由 caller（main）控制
    - 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError）
    - 单股失败不阻塞其他股票
    - 每个 trade_date 创建独立 run 记录（status=running → succeeded/failed）

    [Blocker Fix] scope 传播：
    - scope 注入到每个 run 的 metadata_['scope']（create_snapshot_run + finish_snapshot_run）
    - 'full'：watchlist 可读（默认）
    - 'sample'：watchlist 不可读（--symbols / --limit-instruments 小样本验证）
    - main() 通过 _resolve_run_scope 决定，本函数不重新计算

    dry-run 模式：
    - 只统计 trade_dates / instruments / expected_batches
    - 不调用 load_instrument_bars / compute_feature_snapshot_for_date / upsert_snapshot
    - 不创建 run 记录

    Args:
        db: 异步会话
        trade_dates: 交易日列表
        instruments: instrument_id 列表
        batch_size: 每批 instrument 数（默认 20）
        failure_threshold: 失败比例阈值（默认 0.3）
        resume: 跳过已存在 snapshot 且 succeeded run 的行
        dry_run: 只打印统计，不执行写入
        run_type: run 记录类型（默认 backfill）
        primary_timeframe: 主周期
        secondary_timeframe: 次周期
        adj: 复权方式
        schema_version: 快照 schema 版本
        scope: run 范围（'full' 或 'sample'），默认 'full'

    Returns:
        统计信息 dict
    """
    total_instruments = len(instruments)
    total_dates = len(trade_dates)
    expected_batches = (total_instruments + batch_size - 1) // batch_size if batch_size > 0 else 0

    if dry_run:
        logger.info(
            "[backfill][dry-run] trade_dates=%d, instruments=%d, expected_batches=%d "
            "(batch_size=%d, resume=%s, failure_threshold=%s)",
            total_dates, total_instruments, expected_batches,
            batch_size, resume, failure_threshold,
        )
        return {
            "dry_run": True,
            "trade_dates": total_dates,
            "instruments": total_instruments,
            "expected_batches": expected_batches,
        }

    if total_instruments == 0 or total_dates == 0:
        logger.info(
            "[backfill] 无需处理: trade_dates=%d, instruments=%d",
            total_dates, total_instruments,
        )
        return {
            "dry_run": False,
            "trade_dates": total_dates,
            "instruments": total_instruments,
            "expected_batches": 0,
            "total_snapshots": 0,
            "total_failed": 0,
            "skipped_existing": 0,
        }

    # 为每个 trade_date 创建 running run（publish gate）
    # [Blocker Fix] scope 传入 create_snapshot_run，写入 metadata_['scope']
    run_records: dict[date, StockFeatureSnapshotRun] = {}
    for td in trade_dates:
        try:
            run = await create_snapshot_run(
                db, td, run_type,
                schema_version=schema_version,
                primary_timeframe=primary_timeframe,
                secondary_timeframe=secondary_timeframe,
                adj=adj,
                expected_count=total_instruments,
                metadata={"source": "backfill", "batch_size": batch_size},
                scope=scope,
            )
        except PublishedSnapshotRunExistsError as exc:
            logger.warning(
                "[backfill] trade_date=%s 已存在 published run，跳过: %s",
                td, exc,
            )
            continue
        run_records[td] = run

    # 过滤掉已跳过的 trade_date（published run 已存在）
    trade_dates = [td for td in trade_dates if td in run_records]
    total_dates = len(trade_dates)
    if not trade_dates:
        logger.info("[backfill] 所有 trade_date 均已存在 published run，无需处理")
        return {
            "dry_run": False,
            "trade_dates": 0,
            "instruments": total_instruments,
            "expected_batches": 0,
            "total_snapshots": 0,
            "total_failed": 0,
            "skipped_existing": 0,
        }

    # resume: 查询已 succeeded 的 trade_date + 已存在的 (instrument, date) snapshot
    succeeded_dates: set[date] = set()
    if resume:
        succeeded_dates = await _get_succeeded_trade_dates(db, trade_dates, schema_version=schema_version)

    # per-date 统计
    per_date_stats: dict[date, dict[str, int]] = {
        td: {"success": 0, "failed": 0, "skipped": 0} for td in trade_dates
    }

    # 预查询 resume 模式下每个 trade_date 的已存在 instrument_id 集合
    existing_per_date: dict[date, set[uuid.UUID]] = {}
    if resume:
        for td in trade_dates:
            if td in succeeded_dates:
                existing_per_date[td] = await get_existing_instrument_ids(
                    db, td,
                    primary_timeframe=primary_timeframe,
                    secondary_timeframe=secondary_timeframe,
                    adj=adj,
                    schema_version=schema_version,
                )
            else:
                # run 不是 succeeded，不跳过（允许重试）
                existing_per_date[td] = set()

    # instrument-first 主循环
    _processed_count = 0
    for i in range(0, total_instruments, batch_size):
        batch = instruments[i : i + batch_size]
        for instrument_id in batch:
            _inst_start = time.perf_counter() if profile else None

            # 每只 instrument 只加载一次 bars
            _load_start = time.perf_counter() if profile else None
            primary_bars, secondary_bars = await load_instrument_bars(
                db, instrument_id,
                primary_timeframe=primary_timeframe,
                secondary_timeframe=secondary_timeframe,
                adj=adj,
            )
            if profile and _load_start is not None:
                profile.record("load_bars_ms", (time.perf_counter() - _load_start) * 1000)

            for td in trade_dates:
                # resume 跳过：snapshot 已存在 AND run.status='succeeded'
                if resume and instrument_id in existing_per_date.get(td, set()):
                    per_date_stats[td]["skipped"] += 1
                    continue

                _compute_start = time.perf_counter() if profile else None
                _compute_recorded = False
                try:
                    snapshot = await compute_feature_snapshot_for_date(
                        db, instrument_id, td,
                        primary_timeframe=primary_timeframe,
                        secondary_timeframe=secondary_timeframe,
                        adj=adj,
                        primary_bars=primary_bars,
                        secondary_bars=secondary_bars,
                    )
                    if profile and _compute_start is not None:
                        profile.record(
                            "compute_ms",
                            (time.perf_counter() - _compute_start) * 1000,
                        )
                        _compute_recorded = True
                    _upsert_start = time.perf_counter() if profile else None
                    await upsert_snapshot(db, snapshot)
                    if profile and _upsert_start is not None:
                        profile.record(
                            "upsert_ms",
                            (time.perf_counter() - _upsert_start) * 1000,
                        )
                    per_date_stats[td]["success"] += 1
                except Exception as exc:
                    # [profile-summary] 失败路径也记录 compute_ms 耗时
                    # 仅当 compute_ms 未被记录（compute 失败而非 upsert 失败）
                    if profile and _compute_start is not None and not _compute_recorded:
                        profile.record(
                            "compute_ms",
                            (time.perf_counter() - _compute_start) * 1000,
                        )
                    per_date_stats[td]["failed"] += 1
                    logger.error(
                        "[backfill] snapshot 计算失败 instrument_id=%s trade_date=%s: %s",
                        instrument_id, td, exc, exc_info=True,
                    )

            if profile and _inst_start is not None:
                profile.record(
                    "total_ms_per_instrument",
                    (time.perf_counter() - _inst_start) * 1000,
                )

            # [profile-summary] 每 50 instruments 输出进度摘要
            _processed_count += 1
            if profile and _processed_count % 50 == 0:
                _progress_stats = profile.compute_stats()
                _load_avg = _progress_stats.get("load_bars_ms", {}).get("avg", 0.0)
                _compute_avg = _progress_stats.get("compute_ms", {}).get("avg", 0.0)
                _upsert_avg = _progress_stats.get("upsert_ms", {}).get("avg", 0.0)
                _total_avg = _progress_stats.get(
                    "total_ms_per_instrument", {}
                ).get("avg", 0.0)
                logger.info(
                    "[profile-progress] %d/%d instruments | "
                    "load_avg=%.1fms compute_avg=%.1fms upsert_avg=%.1fms total_avg=%.1fms",
                    _processed_count, total_instruments,
                    _load_avg, _compute_avg, _upsert_avg, _total_avg,
                )

        # 每个 batch 后 flush（不 commit，由 main 控制）
        await db.flush()
        logger.info(
            "[backfill] batch %d/%d 完成 (instruments %d-%d)",
            i // batch_size + 1, expected_batches,
            i + 1, min(i + batch_size, total_instruments),
        )

    # 为每个 trade_date finalize run（succeeded/failed）
    total_snapshots = 0
    total_failed = 0
    total_skipped = 0
    for td in trade_dates:
        stats = per_date_stats[td]
        snapshots = stats["success"]
        failed = stats["failed"]
        skipped = stats["skipped"]
        total_snapshots += snapshots
        total_failed += failed
        total_skipped += skipped

        processed = snapshots + failed
        failure_rate = failed / processed if processed > 0 else 0.0

        if failure_rate > failure_threshold:
            run_status = STATUS_FAILED
        else:
            run_status = STATUS_SUCCEEDED

        # [Blocker Fix] finish 时 metadata 完全替换 create 时的 metadata，
        # 必须再次包含 scope，否则 watchlist gate 会因缺失 scope 而拒绝读取
        await finish_snapshot_run(
            db, run_records[td],
            status=run_status,
            snapshot_count=snapshots,
            failed_count=failed,
            skipped_count=skipped,
            expected_count=total_instruments,
            failure_rate=failure_rate,
            metadata={
                "source": "backfill",
                "batch_size": batch_size,
                "failure_threshold": failure_threshold,
                "scope": scope,
            },
        )
        logger.info(
            "[backfill] trade_date=%s run finalized: status=%s, "
            "snapshots=%d, failed=%d, skipped=%d, failure_rate=%.2f",
            td, run_status, snapshots, failed, skipped, failure_rate,
        )

    logger.info(
        "[backfill] 全部完成: trade_dates=%d, instruments=%d, "
        "total_snapshots=%d, total_failed=%d, total_skipped=%d",
        total_dates, total_instruments,
        total_snapshots, total_failed, total_skipped,
    )

    return {
        "dry_run": False,
        "trade_dates": total_dates,
        "instruments": total_instruments,
        "expected_batches": expected_batches,
        "total_snapshots": total_snapshots,
        "total_failed": total_failed,
        "skipped_existing": total_skipped,
    }


# =============================================================================
# [multiprocessing] - 并行回补
# =============================================================================


def _worker_process_instruments(
    instrument_ids: list[uuid.UUID],
    trade_dates: list[date],
    db_url: str,
    primary_timeframe: str,
    secondary_timeframe: str,
    adj: str,
    resume: bool,
    existing_per_date_str: dict[str, set[str]],
    worker_id: int,
    profile: ProfileCollector | None = None,
) -> dict[str, dict[str, int]] | tuple[dict[str, dict[str, int]], ProfileCollector]:
    """Worker 进程：处理一批 instruments（top-level，可 pickle）。

    [multiprocessing] - 每个 worker 进程独立创建 async engine + session，
    逐 instrument 加载 bars + 计算 + upsert + commit。

    [Blocker Fix] 事务边界（per-date commit）：
    - 每个 (instrument, trade_date) 是独立事务：upsert → commit → success++
    - upsert 异常 → rollback → failed++，后续 date 继续用干净事务
    - commit 失败 → rollback → failed++（success 不增加，DB 无写入）
    - load_bars 失败 → rollback → 该 instrument 所有 dates 标 failed

    [Blocker Fix] DB pool：
    - pool_size=1, max_overflow=0（每 worker 只需 1 session，避免 60 连接）

    [profile-summary] 当传入 profile 时：
    - 对 load_bars / compute / upsert / total_per_instrument 计时
    - 返回 (stats, profile) 元组，主进程 merge 各 worker 的 profile
    - 不传 profile 时返回 dict（向后兼容）

    Args:
        instrument_ids: 本 worker 负责的 instrument UUID 列表
        trade_dates: 交易日列表
        db_url: 数据库连接串（postgresql+psycopg://，内部转 asyncpg）
        primary_timeframe: 主周期
        secondary_timeframe: 次周期
        adj: 复权方式
        resume: 是否跳过已存在
        existing_per_date_str: 已存在 snapshot 的 {td_iso: set(instrument_id_str)}
        worker_id: worker 编号（日志用）
        profile: 可选 ProfileCollector，传入时收集 timing 并随返回值传回主进程

    Returns:
        - profile is None: per-date stats dict（向后兼容）
        - profile is not None: (per-date stats dict, ProfileCollector) 元组
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def _run() -> tuple[dict[str, dict[str, int]], ProfileCollector | None]:
        # worker 进程独立 engine（不能共享主进程的连接池）
        # [Blocker Fix] pool_size=1, max_overflow=0：每 worker 只需 1 session
        async_url = db_url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
        engine = create_async_engine(
            async_url, pool_pre_ping=True, pool_size=1, max_overflow=0,
        )
        session_factory = async_sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False,
        )

        per_date_stats: dict[str, dict[str, int]] = {
            td.isoformat(): {"success": 0, "failed": 0, "skipped": 0}
            for td in trade_dates
        }

        try:
            async with session_factory() as db:
                for idx, instrument_id in enumerate(instrument_ids):
                    instr_id_str = str(instrument_id)
                    _inst_start = time.perf_counter() if profile else None

                    # 1. 加载 bars（失败 → rollback + 所有 dates 标 failed）
                    _load_start = time.perf_counter() if profile else None
                    try:
                        primary_bars, secondary_bars = await load_instrument_bars(
                            db, instrument_id,
                            primary_timeframe=primary_timeframe,
                            secondary_timeframe=secondary_timeframe,
                            adj=adj,
                        )
                    except Exception as exc:
                        if profile and _load_start is not None:
                            profile.record(
                                "load_bars_ms",
                                (time.perf_counter() - _load_start) * 1000,
                            )
                        await db.rollback()
                        logger.error(
                            "[worker-%d] instrument %s load_bars 失败: %s",
                            worker_id, instrument_id, exc,
                        )
                        for td in trade_dates:
                            per_date_stats[td.isoformat()]["failed"] += 1
                        if profile and _inst_start is not None:
                            profile.record(
                                "total_ms_per_instrument",
                                (time.perf_counter() - _inst_start) * 1000,
                            )
                        continue
                    if profile and _load_start is not None:
                        profile.record(
                            "load_bars_ms",
                            (time.perf_counter() - _load_start) * 1000,
                        )

                    # 2. 逐 trade_date 计算 + upsert + per-date commit
                    # [Blocker Fix] per-date 独立事务：
                    #   - upsert 异常 → rollback → failed++，后续 date 继续
                    #   - commit 失败 → rollback → failed++（success 不增加）
                    for td in trade_dates:
                        td_str = td.isoformat()
                        if resume and instr_id_str in existing_per_date_str.get(td_str, set()):
                            per_date_stats[td_str]["skipped"] += 1
                            continue
                        try:
                            _compute_start = time.perf_counter() if profile else None
                            snapshot = await compute_feature_snapshot_for_date(
                                db, instrument_id, td,
                                primary_timeframe=primary_timeframe,
                                secondary_timeframe=secondary_timeframe,
                                adj=adj,
                                primary_bars=primary_bars,
                                secondary_bars=secondary_bars,
                            )
                            if profile and _compute_start is not None:
                                profile.record(
                                    "compute_ms",
                                    (time.perf_counter() - _compute_start) * 1000,
                                )
                            _upsert_start = time.perf_counter() if profile else None
                            await upsert_snapshot(db, snapshot)
                            if profile and _upsert_start is not None:
                                profile.record(
                                    "upsert_ms",
                                    (time.perf_counter() - _upsert_start) * 1000,
                                )
                            # per-date commit：upsert 成功后立即 commit
                            # 失败时 rollback，success 不增加
                            await db.commit()
                            per_date_stats[td_str]["success"] += 1
                        except Exception as exc:
                            # [Blocker Fix] per-date rollback：
                            # 清理事务污染，后续 date 可继续
                            await db.rollback()
                            per_date_stats[td_str]["failed"] += 1
                            logger.error(
                                "[worker-%d] snapshot 失败 instrument=%s date=%s: %s",
                                worker_id, instrument_id, td, exc,
                            )

                    if profile and _inst_start is not None:
                        profile.record(
                            "total_ms_per_instrument",
                            (time.perf_counter() - _inst_start) * 1000,
                        )

                    if (idx + 1) % 10 == 0:
                        logger.info(
                            "[worker-%d] 进度: %d/%d instruments",
                            worker_id, idx + 1, len(instrument_ids),
                        )
        finally:
            await engine.dispose()

        return per_date_stats, profile

    stats, returned_profile = asyncio.run(_run())
    if profile is not None:
        # profile is not None 时 _run() 返回 (stats, ProfileCollector)
        # assert 帮助 mypy 收窄 returned_profile 类型
        assert returned_profile is not None
        return (stats, returned_profile)
    return stats


async def backfill_instrument_first_parallel(
    db: AsyncSession,
    trade_dates: list[date],
    instruments: list[uuid.UUID],
    *,
    workers: int = 4,
    failure_threshold: float = 0.3,
    resume: bool = False,
    run_type: str = "backfill",
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
    scope: str = SCOPE_FULL,
    db_url: str = "",
    profile: ProfileCollector | None = None,
) -> dict[str, Any]:
    """Multiprocessing 版 instrument-first 回补。

    [multiprocessing] - 使用 ProcessPoolExecutor 并行处理 instruments。
    每个 worker 独立 DB session，per-date commit。

    事务边界：
    - run records 在主进程创建 + finalize
    - 每个 worker per-date commit：upsert → db.commit() → success++（commit 成功后才计 success）
    - 异常时 await db.rollback() + failed++，下一 trade_date 继续用干净事务（resume 安全）
    - 单 worker 失败不阻塞其他 workers

    [profile-summary] 当传入 profile 时：
    - 为每个 chunk 创建独立 ProfileCollector 传给 worker
    - worker 返回 (stats, profile) 元组，主进程 merge 到主 profile
    - 不传 profile 时 worker 返回 dict（向后兼容）

    Args:
        db: 主进程 DB 会话（用于创建/finalize run records）
        trade_dates: 交易日列表
        instruments: instrument_id 列表
        workers: 并行进程数
        failure_threshold: 失败比例阈值
        resume: 跳过已存在 snapshot
        run_type: run 记录类型
        primary_timeframe: 主周期
        secondary_timeframe: 次周期
        adj: 复权方式
        schema_version: 快照 schema 版本
        scope: run 范围（'full' 或 'sample'）
        db_url: 数据库连接串（传给 worker 进程）
        profile: 可选 ProfileCollector，传入时合并各 worker 的 timing 样本

    Returns:
        统计信息 dict
    """
    from concurrent.futures import ProcessPoolExecutor

    total_instruments = len(instruments)
    total_dates = len(trade_dates)

    if total_instruments == 0 or total_dates == 0:
        logger.info(
            "[parallel] 无需处理: trade_dates=%d, instruments=%d",
            total_dates, total_instruments,
        )
        return {
            "trade_dates": total_dates,
            "instruments": total_instruments,
            "total_snapshots": 0,
            "total_failed": 0,
            "skipped_existing": 0,
        }

    # 1. 主进程创建 run records（status=running）
    run_records: dict[date, StockFeatureSnapshotRun] = {}
    for td in trade_dates:
        try:
            run = await create_snapshot_run(
                db, td, run_type,
                schema_version=schema_version,
                primary_timeframe=primary_timeframe,
                secondary_timeframe=secondary_timeframe,
                adj=adj,
                expected_count=total_instruments,
                metadata={"source": "backfill", "workers": workers},
                scope=scope,
            )
        except PublishedSnapshotRunExistsError as exc:
            logger.warning(
                "[parallel] trade_date=%s 已存在 published run，跳过: %s",
                td, exc,
            )
            continue
        run_records[td] = run
    await db.commit()  # commit run records，让 workers 可见

    # 过滤掉已跳过的 trade_date（published run 已存在）
    trade_dates = [td for td in trade_dates if td in run_records]
    total_dates = len(trade_dates)
    if not trade_dates:
        logger.info("[parallel] 所有 trade_date 均已存在 published run，无需处理")
        return {
            "trade_dates": 0,
            "instruments": total_instruments,
            "total_snapshots": 0,
            "total_failed": 0,
            "skipped_existing": 0,
        }

    # 2. resume: 查询已 succeeded 的 trade_date + 已存在的 (instrument, date)
    succeeded_dates: set[date] = set()
    if resume:
        succeeded_dates = await _get_succeeded_trade_dates(db, trade_dates, schema_version=schema_version)

    existing_per_date: dict[date, set[uuid.UUID]] = {}
    if resume:
        for td in trade_dates:
            if td in succeeded_dates:
                existing_per_date[td] = await get_existing_instrument_ids(
                    db, td,
                    primary_timeframe=primary_timeframe,
                    secondary_timeframe=secondary_timeframe,
                    adj=adj,
                    schema_version=schema_version,
                )
            else:
                existing_per_date[td] = set()

    # 转为 picklable 格式（str keys/sets）
    existing_per_date_str: dict[str, set[str]] = {
        td.isoformat(): {str(uid) for uid in uids}
        for td, uids in existing_per_date.items()
    }

    # 3. 分发 instruments 到 workers
    chunk_size = max(1, total_instruments // workers)
    chunks: list[list[uuid.UUID]] = []
    for i in range(0, total_instruments, chunk_size):
        chunk = instruments[i : i + chunk_size]
        if chunk:
            chunks.append(chunk)

    logger.info(
        "[parallel] 启动 %d workers, %d instruments 分 %d chunks (chunk_size=%d)",
        workers, total_instruments, len(chunks), chunk_size,
    )

    per_date_stats: dict[date, dict[str, int]] = {
        td: {"success": 0, "failed": 0, "skipped": 0} for td in trade_dates
    }

    # [profile-summary] 为每个 chunk 创建独立 ProfileCollector（pickle 传给 worker）
    chunk_profiles: list[ProfileCollector | None] = [
        ProfileCollector() if profile is not None else None
        for _ in chunks
    ]

    loop = asyncio.get_running_loop()

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = []
        for worker_id, chunk in enumerate(chunks):
            future = loop.run_in_executor(
                executor,
                _worker_process_instruments,
                chunk,
                trade_dates,
                db_url,
                primary_timeframe,
                secondary_timeframe,
                adj,
                resume,
                existing_per_date_str,
                worker_id,
                chunk_profiles[worker_id],
            )
            futures.append(future)

        # [Blocker Fix] 使用 gather(return_exceptions=True) 保留顺序
        # worker 异常时，该 chunk 的 instruments × trade_dates 全部计 failed
        # （不能用 as_completed，Python 3.12 返回 wrapper future，无法映射回 chunk）
        results = await asyncio.gather(*futures, return_exceptions=True)
        for idx, result in enumerate(results):
            chunk = chunks[idx]
            if isinstance(result, BaseException):
                # worker 异常 → 该 chunk 的 instruments × dates 全部 failed
                chunk_len = len(chunk)
                logger.error(
                    "[parallel] worker-%d 失败 (chunk %d instruments): %s",
                    idx, chunk_len, result, exc_info=result,
                )
                for td in trade_dates:
                    per_date_stats[td]["failed"] += chunk_len
            else:
                # [profile-summary] worker 可能返回 (stats, profile) 元组或 dict
                worker_stats: dict[str, dict[str, int]]
                if isinstance(result, tuple):
                    worker_stats, worker_profile = result
                    if profile is not None and worker_profile is not None:
                        profile.merge(worker_profile)
                else:
                    worker_stats = result
                # 合并 stats
                for td_str, stats in worker_stats.items():
                    td = date.fromisoformat(td_str)
                    per_date_stats[td]["success"] += stats["success"]
                    per_date_stats[td]["failed"] += stats["failed"]
                    per_date_stats[td]["skipped"] += stats["skipped"]

    # 4. finalize run records
    total_snapshots = 0
    total_failed = 0
    total_skipped = 0
    for td in trade_dates:
        stats = per_date_stats[td]
        snapshots = stats["success"]
        failed = stats["failed"]
        skipped = stats["skipped"]
        total_snapshots += snapshots
        total_failed += failed
        total_skipped += skipped

        processed = snapshots + failed
        failure_rate = failed / processed if processed > 0 else 0.0
        run_status = STATUS_FAILED if failure_rate > failure_threshold else STATUS_SUCCEEDED

        await finish_snapshot_run(
            db, run_records[td],
            status=run_status,
            snapshot_count=snapshots,
            failed_count=failed,
            skipped_count=skipped,
            expected_count=total_instruments,
            failure_rate=failure_rate,
            metadata={
                "source": "backfill",
                "workers": workers,
                "failure_threshold": failure_threshold,
                "scope": scope,
            },
        )
        logger.info(
            "[parallel] trade_date=%s run finalized: status=%s, "
            "snapshots=%d, failed=%d, skipped=%d, failure_rate=%.2f",
            td, run_status, snapshots, failed, skipped, failure_rate,
        )

    await db.commit()
    logger.info(
        "[parallel] 全部完成: trade_dates=%d, instruments=%d, "
        "total_snapshots=%d, total_failed=%d, total_skipped=%d",
        total_dates, total_instruments,
        total_snapshots, total_failed, total_skipped,
    )

    return {
        "trade_dates": total_dates,
        "instruments": total_instruments,
        "total_snapshots": total_snapshots,
        "total_failed": total_failed,
        "skipped_existing": total_skipped,
    }


async def main(args: argparse.Namespace) -> None:
    """主入口：解析参数，加载 trade_dates / instruments，调用 instrument-first 回补。

    事务边界：
    - 单 session 控制整个回补
    - backfill_instrument_first 不内部 commit
    - 成功 → commit；异常 → rollback
    """
    start_date = date.fromisoformat(args.start)

    # 解析 end 日期 + 获取交易日列表 + 获取 instruments（使用临时 session）
    async with AsyncSessionLocal() as db:
        if args.end == "latest":
            end_date = await get_latest_bar_date(db)
            if end_date is None:
                logger.error("[backfill] bars_daily 表无数据，无法确定 end 日期")
                sys.exit(1)
            logger.info("[backfill] end=latest → 使用 bars_daily 最新日期: %s", end_date)
        else:
            end_date = date.fromisoformat(args.end)

        if start_date > end_date:
            logger.error(
                "[backfill] start (%s) > end (%s)", start_date, end_date,
            )
            sys.exit(1)

        # 从 bars_daily 获取实际有数据的交易日
        trade_dates = await get_trade_dates_from_bars(db, start_date, end_date)
        if not trade_dates:
            logger.error(
                "[backfill] bars_daily 表在 %s ~ %s 之间无数据",
                start_date, end_date,
            )
            sys.exit(1)

        # 获取 instruments（支持 --symbols / --limit-instruments）
        instruments = await get_instruments_for_backfill(
            db,
            symbols=args.symbols,
            limit=args.limit_instruments,
        )

        # [Blocker Fix] 根据 --symbols / --limit-instruments 决定 run scope：
        # - 任一过滤条件启用 → 'sample'（小样本验证，watchlist 不可读）
        # - 都未启用 → 'full'（全市场，watchlist 可读）
        scope = _resolve_run_scope(args.symbols, args.limit_instruments)

        logger.info(
            "[backfill] 计划回补: trade_dates=%d (%s ~ %s), instruments=%d "
            "(batch_size=%d, resume=%s, dry_run=%s, failure_threshold=%s, "
            "symbols=%s, limit_instruments=%s, scope=%s)",
            len(trade_dates),
            trade_dates[0], trade_dates[-1],
            len(instruments),
            args.batch_size,
            args.resume, args.dry_run, args.failure_threshold,
            args.symbols, args.limit_instruments, scope,
        )

        # dry-run 在临时 session 内完成统计（不写库、不创建 run）
        if args.dry_run:
            await backfill_instrument_first(
                db,
                trade_dates=trade_dates,
                instruments=instruments,
                batch_size=args.batch_size,
                failure_threshold=args.failure_threshold,
                resume=args.resume,
                dry_run=True,
                scope=scope,
            )
            return

    # [profile-summary] 启用时创建 ProfileCollector，传给 backfill，结束时打印聚合统计
    profile = ProfileCollector() if args.profile_summary else None

    # 非干跑模式：新开 session 执行回补
    if not args.dry_run:
        # [multiprocessing] workers > 1 时使用并行模式
        if args.workers > 1:
            from app.config import get_settings

            db_url = get_settings().database_url
            logger.info(
                "[backfill] 并行模式: workers=%d, db_url=%s",
                args.workers, db_url.split("@")[-1] if "@" in db_url else "(masked)",
            )
            async with AsyncSessionLocal() as db:
                try:
                    result = await backfill_instrument_first_parallel(
                        db,
                        trade_dates=trade_dates,
                        instruments=instruments,
                        workers=args.workers,
                        failure_threshold=args.failure_threshold,
                        resume=args.resume,
                        scope=scope,
                        db_url=db_url,
                        profile=profile,
                    )
                    logger.info(
                        "[backfill][parallel] 完成: total_snapshots=%d, "
                        "total_failed=%d, skipped_existing=%d",
                        result.get("total_snapshots", 0),
                        result.get("total_failed", 0),
                        result.get("skipped_existing", 0),
                    )
                except Exception as exc:
                    logger.error(
                        "[backfill][parallel] 失败: %s", exc, exc_info=True,
                    )
                    raise
        else:
            async with AsyncSessionLocal() as db:
                try:
                    result = await backfill_instrument_first(
                        db,
                        trade_dates=trade_dates,
                        instruments=instruments,
                        batch_size=args.batch_size,
                        failure_threshold=args.failure_threshold,
                        resume=args.resume,
                        dry_run=False,
                        scope=scope,
                        profile=profile,
                    )
                    await db.commit()
                    logger.info(
                        "[backfill] 提交完成: total_snapshots=%d, total_failed=%d, "
                        "skipped_existing=%d",
                        result.get("total_snapshots", 0),
                        result.get("total_failed", 0),
                        result.get("skipped_existing", 0),
                    )
                except Exception as exc:
                    await db.rollback()
                    logger.error(
                        "[backfill] 回补失败，已 rollback: %s", exc, exc_info=True,
                    )
                    raise

    # [profile-summary] 结束时打印聚合统计到 stdout
    if profile is not None:
        print(profile.format_summary(
            instruments_total=len(instruments),
            trade_dates_total=len(trade_dates),
            rows_new=result.get("total_snapshots", 0),
            rows_skipped=result.get("skipped_existing", 0),
            rows_failed=result.get("total_failed", 0),
            worker_count=args.workers,
        ))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="特征快照历史回补脚本（instrument-first）",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="起始日期（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--end",
        default="latest",
        help='结束日期（YYYY-MM-DD 或 "latest"，默认 latest）',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="每批 instrument 数（默认 20，保守内存）",
    )
    parser.add_argument(
        "--failure-threshold",
        type=float,
        default=0.3,
        help="失败比例阈值（默认 0.3，超过则该 trade_date 标 run.status='failed'）",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="只处理指定股票代码（逗号分隔，如 000100,603303）",
    )
    parser.add_argument(
        "--limit-instruments",
        type=int,
        default=None,
        help="限制处理的 instrument 数量（用于小样本验证）",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="跳过已存在 snapshot 且所属 trade_date 的 run.status='succeeded' 的行",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与统计，不执行写入",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并行进程数（默认 1 单进程，>1 启用 multiprocessing；建议 = CPU 核数）",
    )
    parser.add_argument(
        "--profile-summary",
        action="store_true",
        default=False,
        help="启用轻量性能诊断：只在 stdout 输出聚合统计（total/avg/p50/p95），不写文件、不输出逐股票明细",
    )
    args = parser.parse_args()
    # [Blocker Fix] workers 参数保护
    # 1. workers < 1 → 拒绝（argparse error → SystemExit）
    if args.workers < 1:
        parser.error(f"--workers 必须 >= 1，实际: {args.workers}")
    # 2. workers > cpu_count → cap + warning
    #    防止用户误设过大值导致进程调度抖动
    cpu_count = os.cpu_count() or 1
    if args.workers > cpu_count:
        warnings.warn(
            f"--workers={args.workers} > cpu_count={cpu_count}，自动 cap 到 {cpu_count}",
            stacklevel=2,
        )
        args.workers = cpu_count
    # 解析 --symbols 为 list[str]
    if args.symbols is not None:
        args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return args


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
