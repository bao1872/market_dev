"""复权因子全市场一致性审计脚本（CHANGE-20260718-007 S3.1）。

用法（容器内）：
    python /app/scripts/run_factor_audit.py
    python /app/scripts/run_factor_audit.py --rebuild
    python /app/scripts/run_factor_audit.py --symbols 603629,603538,000725,600276
    python /app/scripts/run_factor_audit.py --rebuild --symbols 603629

用法（宿主机通过 stdin 管道，不修改容器文件系统）：
    docker exec -i trading-backend python - < backend/scripts/run_factor_audit.py
    docker exec -i trading-backend python - < backend/scripts/run_factor_audit.py -- --rebuild

功能：
1. dry-run（默认）：全市场只读审计，输出 total_audited / consistent / needs_rebuild / errors
2. --rebuild：审计发现不一致时，按小批次串行重建，记录 before/after hash
3. --symbols：仅审计指定股票（用于样本验证）
4. --output：报告输出 JSON 路径（默认 /tmp/factor_audit_report_<timestamp>.json）

输出（PROMPT.md S3.1 要求的真实数字）：
- 全市场审计股票总数
- mismatch 股票数
- 实际重建成功数
- 失败清单（symbol + error_code + error_message）
- 样本股（利通电子/美诺华/京东方A/恒瑞医药）before/after hash

安全约束：
- dry-run 零副作用（不写库、不失效缓存）
- rebuild 全程串行（禁止并发），每只股票独立事务
- 失败不写 1.0 伪装成功
- 不做无边界全市场重跑（只重建审计发现的不一致股票）

S3.1 接入说明：
- 本脚本提供"按需全市场审计"能力，产出真实数字
- 生产闭环接入由 bars_scheduler_service._audit_and_rebuild_factors 实现
  （在 _rebuild_factors_if_needed 之后、_check_daily_coverage_and_trigger_dsa 之前）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

# 确保可以 import app.*（容器内 WORKDIR=/app，宿主机调试时需手动 cd）
sys.path.insert(0, "/root/web_dev/backend")

from app.db import AsyncSessionLocal  # noqa: E402
from app.services.factor_consistency_audit import FactorConsistencyAuditor  # noqa: E402
from app.services.factor_reconciliation import FactorReconciliationTask  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("run_factor_audit")

# PROMPT.md S3.1 明确要求的样本股（利通电子/美诺华/京东方A/恒瑞医药对照）
_SAMPLE_SYMBOLS = ["603629", "603538", "000725", "600276"]


async def _audit_sample_stocks(symbols: list[str]) -> dict[str, dict]:
    """对样本股单独审计，输出 before hash 供 PROMPT.md 要求的"修复前后因子结果"。"""
    auditor = FactorConsistencyAuditor()
    from sqlalchemy import select

    from app.models.instrument import Instrument

    samples: dict[str, dict] = {}
    async with AsyncSessionLocal() as session:
        for symbol in symbols:
            row = (
                await session.execute(
                    select(Instrument.id, Instrument.name).where(
                        Instrument.symbol == symbol
                    )
                )
            ).first()
            if row is None:
                samples[symbol] = {"error": "instrument_not_found"}
                continue
            result = await auditor.audit_single_stock(
                session, row.id, symbol, max_mismatches=10,
            )
            samples[symbol] = {
                "name": row.name,
                "is_consistent": result.is_consistent,
                "stored_count": result.stored_count,
                "expected_count": result.expected_count,
                "mismatch_count": result.mismatch_count,
                "missing_factor_count": result.missing_factor_count,
                "factor_all_unit_with_events": result.factor_all_unit_with_events,
                "stored_factor_hash": result.stored_factor_hash,
                "expected_factor_hash": result.expected_factor_hash,
                "earliest_mismatch": (
                    result.earliest_mismatch.isoformat()
                    if result.earliest_mismatch
                    else None
                ),
                "error": result.error,
                "mismatches_preview": [
                    {
                        "trade_date": m.trade_date.isoformat(),
                        "stored": m.stored_factor,
                        "expected": m.expected_factor,
                        "diff": m.diff,
                    }
                    for m in result.mismatches[:5]
                ],
            }
    return samples


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="复权因子全市场一致性审计（S3.1）",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="审计发现不一致时串行重建（默认只 dry-run）",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="仅审计指定股票（逗号分隔，如 603629,603538）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="审计分批大小（默认 50）",
    )
    parser.add_argument(
        "--rebuild-batch-size",
        type=int,
        default=10,
        help="重建分批大小（默认 10）",
    )
    parser.add_argument(
        "--max-mismatches",
        type=int,
        default=20,
        help="每只股票 mismatch 明细最大条数（默认 20）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="报告输出 JSON 路径（默认 /tmp/factor_audit_report_<timestamp>.json）",
    )
    # stdin 管道模式下 sys.argv[0] == '-'，argparse 仍能正常解析后续 --flag
    args = parser.parse_args()

    symbols = (
        [s.strip() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else None
    )

    started_at = datetime.now(UTC)
    logger.info(
        "[S3.1] 因子审计开始: mode=%s symbols=%s rebuild=%s",
        "specified" if symbols else "full_market",
        symbols or "ALL",
        args.rebuild,
    )

    task = FactorReconciliationTask()

    # =========================================================================
    # Phase 1: dry-run 审计
    # =========================================================================
    async with AsyncSessionLocal() as session:
        plan = await task.dry_run(
            session,
            symbols=symbols,
            batch_size=args.batch_size,
            max_mismatches=args.max_mismatches,
        )

    logger.info(
        "[S3.1] dry-run 完成: total_audited=%d consistent=%d needs_rebuild=%d errors=%d",
        plan.total_audited, plan.consistent_count,
        plan.needs_rebuild_count, plan.error_count,
    )

    # 收集 needs_rebuild 的股票清单（symbol + before_hash + reason）
    needs_rebuild_list = [
        {
            "symbol": item.symbol,
            "instrument_id": str(item.instrument_id),
            "earliest_affected": item.earliest_affected.isoformat(),
            "before_hash": item.before_hash,
            "mismatch_count": item.mismatch_count,
            "reason": item.reason,
        }
        for item in plan.items
    ]

    if plan.needs_rebuild_count > 0:
        logger.warning(
            "[S3.1] 发现 %d 只不一致股票: %s",
            plan.needs_rebuild_count,
            [i["symbol"] for i in needs_rebuild_list[:20]],
        )
        if plan.needs_rebuild_count > 20:
            logger.warning("[S3.1] ... 及其余 %d 只",
                           plan.needs_rebuild_count - 20)

    # =========================================================================
    # Phase 2: 串行重建（仅 --rebuild 且发现不一致时执行）
    # =========================================================================
    rebuild_report = None
    if args.rebuild and plan.needs_rebuild_count > 0:
        logger.info(
            "[S3.1] 开始串行重建 %d 只股票（batch_size=%d）",
            plan.needs_rebuild_count, args.rebuild_batch_size,
        )
        async with AsyncSessionLocal() as session:
            report = await task.rebuild_batch(
                session, plan, batch_size=args.rebuild_batch_size,
            )

        rebuild_report = {
            "total_planned": report.total_planned,
            "success_count": report.success_count,
            "failure_count": report.failure_count,
            "is_all_success": report.is_all_success,
            "success_rate": report.success_rate,
            "started_at": report.started_at.isoformat(),
            "finished_at": report.finished_at.isoformat(),
            "results": [
                {
                    "symbol": r.symbol,
                    "instrument_id": str(r.instrument_id),
                    "success": r.success,
                    "before_hash": r.before_hash,
                    "after_hash": r.after_hash,
                    "records_updated": r.records_updated,
                    "error_code": r.error_code,
                    "error_message": r.error_message,
                    "rebuilt_at": r.rebuilt_at.isoformat(),
                }
                for r in report.results
            ],
        }
        logger.info(
            "[S3.1] 重建完成: total=%d success=%d failure=%d rate=%.4f",
            report.total_planned, report.success_count,
            report.failure_count, report.success_rate,
        )
        if report.failure_count > 0:
            failed = [r for r in report.results if not r.success]
            logger.warning(
                "[S3.1] 重建失败清单: %s",
                [
                    {"symbol": r.symbol, "error_code": r.error_code}
                    for r in failed
                ],
            )
    elif args.rebuild and plan.needs_rebuild_count == 0:
        logger.info("[S3.1] --rebuild 模式但未发现不一致股票，跳过重建")

    # =========================================================================
    # Phase 3: 样本股 before/after hash（PROMPT.md 明确要求）
    # =========================================================================
    # 重建后重新审计样本股，输出 after hash
    sample_symbols = symbols if symbols else _SAMPLE_SYMBOLS
    logger.info(
        "[S3.1] 样本股审计: %s",
        sample_symbols,
    )
    samples_after = await _audit_sample_stocks(sample_symbols)

    # =========================================================================
    # Phase 4: 汇总报告
    # =========================================================================
    finished_at = datetime.now(UTC)
    duration_seconds = (finished_at - started_at).total_seconds()

    report_data = {
        "script": "run_factor_audit.py",
        "section": "S3.1",
        "change_record": "CHANGE-20260718-007",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": duration_seconds,
        "mode": "specified" if symbols else "full_market",
        "symbols": symbols,
        "rebuild_enabled": args.rebuild,
        "dry_run": {
            "total_audited": plan.total_audited,
            "consistent_count": plan.consistent_count,
            "needs_rebuild_count": plan.needs_rebuild_count,
            "error_count": plan.error_count,
            "algorithm_version": plan.algorithm_version,
            "reconciliation_version": plan.reconciliation_version,
            "dry_run_at": plan.dry_run_at.isoformat(),
            "needs_rebuild_list": needs_rebuild_list,
        },
        "rebuild": rebuild_report,
        "samples": samples_after,
    }

    # 输出 JSON 报告
    output_path = args.output or (
        f"/tmp/factor_audit_report_{started_at.strftime('%Y%m%d_%H%M%S')}.json"
    )
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        json.dumps(report_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[S3.1] 报告已写入: %s", output_path)

    # stdout 摘要（便于 docker exec 捕获）
    print("=" * 72)
    print("[S3.1] 复权因子全市场一致性审计 - 摘要")
    print("=" * 72)
    print(f"模式: {'指定股票' if symbols else '全市场'}"
          f"{'（含重建）' if args.rebuild else '（dry-run）'}")
    print(f"耗时: {duration_seconds:.1f}s")
    print(f"审计总数: {plan.total_audited}")
    print(f"一致股票: {plan.consistent_count}")
    print(f"不一致需重建: {plan.needs_rebuild_count}")
    print(f"审计错误: {plan.error_count}")
    if rebuild_report:
        print(f"重建成功: {rebuild_report['success_count']}"
              f"/{rebuild_report['total_planned']}")
        print(f"重建失败: {rebuild_report['failure_count']}")
    print("-" * 72)
    print("样本股（PROMPT.md 要求）:")
    for sym, info in samples_after.items():
        if info.get("error") == "instrument_not_found":
            print(f"  {sym}: NOT_FOUND")
            continue
        status = "一致" if info.get("is_consistent") else "不一致"
        audit_err = info.get("error") or ""
        err_suffix = f" err={audit_err}" if audit_err else ""
        print(f"  {sym} ({info.get('name', '?')}): {status}"
              f" mismatch={info.get('mismatch_count', '?')}"
              f" hash={info.get('stored_factor_hash', '?')[:8]}..."
              f"{err_suffix}")
    print("-" * 72)
    print(f"完整报告: {output_path}")
    print("=" * 72)

    # 退出码：0=全一致或重建全成功；1=有失败
    has_failures = (
        plan.error_count > 0
        or (rebuild_report and rebuild_report["failure_count"] > 0)
    )
    return 1 if has_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
