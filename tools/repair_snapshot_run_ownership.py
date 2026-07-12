"""修复 stock_feature_snapshots.source_run_id 归属（默认 dry-run）。

问题背景：
    早期写入的快照 source_run_id=NULL，导致 stock_context API 精确查询失败，
    返回 state=null。本脚本按唯一约束字段回退匹配 canonical succeeded+published+full run，
    仅当候选 run 唯一时补写 source_run_id；多候选或无候选不猜，分别报告 ambiguous / orphan。

用法：
    # dry-run（默认，只报告）
    DATABASE_URL="postgresql+psycopg://bz:***@127.0.0.1:5432/bz_stock" \
    backend/.venv/bin/python tools/repair_snapshot_run_ownership.py

    # 实际写入（需显式 --apply）
    DATABASE_URL="..." backend/.venv/bin/python tools/repair_snapshot_run_ownership.py --apply

匹配规则（对每条 source_run_id IS NULL 的快照）：
    按 (trade_date, schema_version, primary_timeframe, secondary_timeframe, adj)
    查找 stock_feature_snapshot_runs 中 status='succeeded' AND published_at IS NOT NULL
    AND metadata_->>'scope' = 'full' 的候选 run。
    - 候选数 == 1 → repairable（--apply 时补 source_run_id）
    - 候选数 == 0 → orphan（无 canonical run，跳过）
    - 候选数  > 1 → ambiguous（不猜，跳过）

约束：
    - 不修改 payload、不删除快照、不创建新 run
    - --apply 只 UPDATE source_run_id 单列
    - 不修改已归属的快照（WHERE source_run_id IS NULL）
    - 不扫描 Redis、不写历史 JSON
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from uuid import UUID

# 将 backend 目录加入 sys.path
_BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
_BACKEND_DIR = os.path.abspath(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)

from sqlalchemy import and_, select, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.db import AsyncSessionLocal  # noqa: E402
from app.models.stock_feature_snapshot import StockFeatureSnapshot  # noqa: E402
from app.models.stock_feature_snapshot_run import (  # noqa: E402
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)


@dataclass
class RepairReport:
    """修复结果汇总。"""

    total_null: int = 0
    repairable: int = 0
    orphan: int = 0
    ambiguous: int = 0
    applied: int = 0
    ambiguous_samples: list[str] = field(default_factory=list)  # 前 10 条样本
    orphan_trade_dates: list[str] = field(default_factory=list)


async def _find_null_snapshots(
    session: AsyncSession,
) -> list[StockFeatureSnapshot]:
    """查询所有 source_run_id IS NULL 的快照（按 trade_date, instrument_id 排序）。"""
    stmt = (
        select(StockFeatureSnapshot)
        .where(StockFeatureSnapshot.source_run_id.is_(None))
        .order_by(
            StockFeatureSnapshot.trade_date.desc(),
            StockFeatureSnapshot.instrument_id,
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _find_candidate_runs(
    session: AsyncSession,
    snapshot: StockFeatureSnapshot,
) -> list[StockFeatureSnapshotRun]:
    """按唯一约束字段查找 canonical succeeded+published+full 候选 run。"""
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            and_(
                StockFeatureSnapshotRun.trade_date == snapshot.trade_date,
                StockFeatureSnapshotRun.schema_version == snapshot.schema_version,
                StockFeatureSnapshotRun.primary_timeframe == snapshot.primary_timeframe,
                StockFeatureSnapshotRun.secondary_timeframe == snapshot.secondary_timeframe,
                StockFeatureSnapshotRun.adj == snapshot.adj,
                StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
                StockFeatureSnapshotRun.published_at.is_not(None),
                StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
            )
        )
        .order_by(
            StockFeatureSnapshotRun.published_at.desc(),
            StockFeatureSnapshotRun.finished_at.desc(),
        )
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _apply_repair(
    session: AsyncSession,
    snapshot_ids: list[UUID],
    run_id: UUID,
) -> int:
    """批量 UPDATE source_run_id（仅对指定快照 ID）。"""
    if not snapshot_ids:
        return 0
    stmt = (
        update(StockFeatureSnapshot)
        .where(
            and_(
                StockFeatureSnapshot.id.in_(snapshot_ids),
                StockFeatureSnapshot.source_run_id.is_(None),
            )
        )
        .values(source_run_id=run_id)
    )
    result = await session.execute(stmt)
    # CursorResult.rowcount 在 SQLAlchemy 2.0 类型 stubs 中未暴露，用 getattr 安全访问
    return getattr(result, "rowcount", 0) or 0


async def _run_repair(apply: bool) -> RepairReport:
    """执行修复主流程。"""
    report = RepairReport()

    async with AsyncSessionLocal() as session:
        null_snapshots = await _find_null_snapshots(session)
        report.total_null = len(null_snapshots)

        if not null_snapshots:
            return report

        # 按唯一约束字段分组，减少重复查询（同 trade_date+参数的快照共享候选 run）
        # key = (trade_date, schema_version, primary_tf, secondary_tf, adj)
        # value = list[snapshot]
        groups: dict[tuple, list[StockFeatureSnapshot]] = {}
        for snap in null_snapshots:
            key = (
                snap.trade_date,
                snap.schema_version,
                snap.primary_timeframe,
                snap.secondary_timeframe,
                snap.adj,
            )
            groups.setdefault(key, []).append(snap)

        for key, snapshots_in_group in groups.items():
            trade_date, schema_ver, ptf, stf, adj = key
            # 用组内第一条快照查候选 run（同组共享）
            candidates = await _find_candidate_runs(session, snapshots_in_group[0])
            group_size = len(snapshots_in_group)

            if len(candidates) == 0:
                report.orphan += group_size
                td_str = trade_date.isoformat() if trade_date else "None"
                if td_str not in report.orphan_trade_dates:
                    report.orphan_trade_dates.append(td_str)
                continue

            if len(candidates) > 1:
                report.ambiguous += group_size
                if len(report.ambiguous_samples) < 10:
                    run_ids = ", ".join(str(r.id) for r in candidates[:3])
                    sample = (
                        f"trade_date={trade_date} schema={schema_ver} "
                        f"tf={ptf}/{stf} adj={adj} -> "
                        f"{len(candidates)} candidate runs ({run_ids}...)"
                    )
                    report.ambiguous_samples.append(sample)
                continue

            # 唯一候选 → repairable
            report.repairable += group_size
            if apply:
                snapshot_ids = [s.id for s in snapshots_in_group]
                updated = await _apply_repair(session, snapshot_ids, candidates[0].id)
                report.applied += updated

        if apply and report.applied > 0:
            await session.commit()
        else:
            # dry-run 或无可修复项，回滚任何可能的 pending 状态
            await session.rollback()

    return report


def _print_report(report: RepairReport, apply: bool) -> None:
    """打印修复报告。"""
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n{'=' * 60}")
    print(f"repair_snapshot_run_ownership [{mode}]")
    print(f"{'=' * 60}")
    print(f"  source_run_id IS NULL 总数: {report.total_null}")
    print(f"  可修复（唯一候选 run）:    {report.repairable}")
    print(f"  孤儿（无 canonical run）:  {report.orphan}")
    print(f"  歧义（多候选 run）:        {report.ambiguous}")
    if apply:
        print(f"  实际写入 source_run_id:    {report.applied}")
    print()

    if report.orphan_trade_dates:
        print(f"孤儿 trade_date（无 canonical run，{len(report.orphan_trade_dates)} 个）:")
        for td in report.orphan_trade_dates[:10]:
            print(f"  - {td}")
        if len(report.orphan_trade_dates) > 10:
            print(f"  ... 共 {len(report.orphan_trade_dates)} 个")
        print()

    if report.ambiguous_samples:
        print(f"歧义样本（前 {len(report.ambiguous_samples)} 条，需人工排查）:")
        for sample in report.ambiguous_samples:
            print(f"  - {sample}")
        print()

    if apply:
        if report.applied == report.repairable:
            print(f"✓ 全部 {report.applied} 条可修复快照已写入 source_run_id")
        else:
            print(
                f"⚠ 预期修复 {report.repairable} 条，实际写入 {report.applied} 条"
                "（可能被并发修改）"
            )
    else:
        if report.repairable > 0:
            print(
                f"→ dry-run 检测到 {report.repairable} 条可修复快照，"
                "使用 --apply 实际写入"
            )
        else:
            print("→ 无可修复快照（全部为孤儿或歧义）")

    if report.ambiguous > 0:
        print(
            f"⚠ {report.ambiguous} 条歧义快照未修复，"
            "需人工确认后单独处理（不猜归属）"
        )
    print(f"{'=' * 60}\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修复 stock_feature_snapshots.source_run_id 归属（默认 dry-run）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入 source_run_id（默认 dry-run，只报告）",
    )
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("错误：必须设置 DATABASE_URL 环境变量", file=sys.stderr)
        return 2

    report = asyncio.run(_run_repair(apply=args.apply))
    _print_report(report, apply=args.apply)

    # 歧义或孤儿不算失败（脚本正确报告了它们），只在实际写入失败时返回非 0
    if args.apply and report.applied != report.repairable:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
