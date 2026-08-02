"""Review Bootstrap CLI - 从 canonical PIT 历史回填 scope observations（正式运维入口）。

与 admin API 的分工：
    - Admin API（POST /v1/admin/review/bootstrap）异步提交，返回 202 + job_run_id，
      由 Worker 领取执行，适合从页面触发并轮询进度。
    - 本 CLI 同步执行并等待结果，适合运维在服务器上一次性核对与回填。
    两者共用同一个 review_bootstrap_service.bootstrap_history 入口，行为一致。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.review_bootstrap_cli \\
        --days-back 120 --operator ops --reason "review-2.0.0 历史回填" --dry-run

    # 实际写入（需管理员确认）
    .venv/bin/python -m scripts.review_bootstrap_cli --days-back 120 \\
        --operator ops --reason "review-2.0.0 历史回填" --no-dry-run

    # 指定截止日期与算法版本
    .venv/bin/python -m scripts.review_bootstrap_cli --end-date 2026-07-25 \\
        --algorithm-version review-2.0.0 --operator ops --reason 补历史 --no-dry-run

参数：
    --days-back: 回溯天数（默认 120，最低 60）
    --dry-run / --no-dry-run: 只计算不写入（默认 dry-run）
    --end-date: 截止交易日（YYYY-MM-DD；缺省=最近一个完整 A 股交易日）
    --operator: 执行人标识（必填，审计用）
    --reason: 执行原因（必填，审计用）
    --algorithm-version: 显式算法版本（缺省=当前 REVIEW_ALGORITHM_VERSION）
    --summary-only: 只打印摘要与四类计数，不打印逐日明细

退出码：
    0: 成功
    1: 无可 bootstrap 的日期
    2: 参数错误
    3: 执行失败（存在 failed scope 或服务层报错）

约束：
    - 默认 dry-run，且 dry-run **零业务写入**（不建 run、不写 observation、不切 pointer）
    - 幂等：相同 trade_date 已有 bootstrap run 时复用，不重复写入
    - 不修改 stock_core 数据（只读）
    - 不修改现有 review run（只创建/复用 bootstrap run）
    - 不绕过 publish gate（bootstrap 只补历史，不 force publish）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date

logger = logging.getLogger("review_bootstrap_cli")

MIN_DAYS_BACK = 60

# [FIX-20260802] 内存分片默认值。
# 与 app.services.review_bootstrap_service 中的同名常量保持一致，
# 在此本地定义是为了让 --help 不依赖 app 运行时（模块导入被延迟到 _run 内）。
# 一致性由 _assert_defaults_in_sync() 在实际执行前校验，防止两处默默漂移。
DEFAULT_BOOTSTRAP_CHUNK_DAYS = 5
DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB = 1536


def _assert_defaults_in_sync() -> None:
    """校验 CLI 默认值与 service 常量一致，避免两处定义漂移。"""
    from app.services import review_bootstrap_service as svc

    mismatches = []
    if DEFAULT_BOOTSTRAP_CHUNK_DAYS != svc.DEFAULT_BOOTSTRAP_CHUNK_DAYS:
        mismatches.append(
            f"chunk_days CLI={DEFAULT_BOOTSTRAP_CHUNK_DAYS} "
            f"service={svc.DEFAULT_BOOTSTRAP_CHUNK_DAYS}",
        )
    if DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB != svc.DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB:
        mismatches.append(
            f"memory_budget_mb CLI={DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB} "
            f"service={svc.DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB}",
        )
    if mismatches:
        raise RuntimeError(
            "CLI 与 service 的 bootstrap 内存默认值不一致: " + "; ".join(mismatches),
        )


def _format_summary(result: dict) -> str:
    """把执行结果渲染为人可读摘要（四类计数 + 原因码）。"""
    counts = result.get("scope_counts", {})
    reason_codes = result.get("reason_codes", {})
    lines = [
        "===== Review Bootstrap 摘要 =====",
        f"  dry_run           : {result.get('dry_run')}",
        f"  end_date          : {result.get('end_date')}",
        f"  days_back         : {result.get('days_back')}",
        f"  algorithm_version : {result.get('algorithm_version')}",
        f"  operator          : {result.get('operator')}",
        f"  reason            : {result.get('reason')}",
        f"  input_hash        : {result.get('input_hash')}",
        f"  eligible_dates    : {result.get('eligible_dates')}",
        f"  processed         : {result.get('processed')}",
        f"  skipped           : {result.get('skipped')}",
        f"  written           : {result.get('written')}",
        f"  status            : {result.get('status')}",
        f"  chunks            : {result.get('chunks')}",
        f"  peak_rss_mb       : {result.get('peak_rss_mb')}",
        "  scope_counts:",
        f"    succeeded   : {counts.get('succeeded', 0)}",
        f"    skipped     : {counts.get('skipped', 0)}",
        f"    unavailable : {counts.get('unavailable', 0)}",
        f"    failed      : {counts.get('failed', 0)}",
    ]
    if reason_codes:
        lines.append("  reason_codes:")
        for code, count in sorted(
            reason_codes.items(), key=lambda kv: (-kv[1], kv[0]),
        ):
            lines.append(f"    {code}: {count}")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    from app.db import AsyncSessionLocal
    from app.services.review_bootstrap_service import bootstrap_history

    _assert_defaults_in_sync()

    end_date = None
    if args.end_date:
        try:
            end_date = date.fromisoformat(args.end_date)
        except ValueError:
            print(f"错误: --end-date 格式应为 YYYY-MM-DD: {args.end_date}", file=sys.stderr)
            return 2

    if args.days_back < MIN_DAYS_BACK:
        print(
            f"错误: --days-back 最低 {MIN_DAYS_BACK}，当前 {args.days_back}",
            file=sys.stderr,
        )
        return 2

    if not args.operator.strip():
        print("错误: --operator 不得为空（审计要求）", file=sys.stderr)
        return 2
    if not args.reason.strip():
        print("错误: --reason 不得为空（审计要求）", file=sys.stderr)
        return 2

    async with AsyncSessionLocal() as session:
        try:
            result = await bootstrap_history(
                session,
                end_date=end_date,
                days_back=args.days_back,
                dry_run=args.dry_run,
                algorithm_version=args.algorithm_version,
                operator=args.operator.strip(),
                reason=args.reason.strip(),
                chunk_days=args.chunk_days,
                memory_budget_mb=args.memory_budget_mb,
            )
        except ValueError as exc:
            # 服务层参数校验失败（如 algorithm_version 不匹配）
            print(f"错误: {exc}", file=sys.stderr)
            await session.rollback()
            return 2

        if args.dry_run:
            # dry-run 严格零业务写入：显式回滚兜底，绝不 commit
            await session.rollback()
        else:
            await session.commit()

        print(_format_summary(result))
        if not args.summary_only:
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))

        if result.get("eligible_dates", 0) == 0:
            print("WARNING: 无可 bootstrap 的日期", file=sys.stderr)
            return 1

        # 内存预算触顶：如实以失败退出，绝不当作成功
        if result.get("status") == "memory_budget_exceeded":
            print(
                f"ERROR: RSS 峰值 {result.get('peak_rss_mb')}MB 超出预算 "
                f"{args.memory_budget_mb}MB，已在处理 {result.get('processed')}/"
                f"{result.get('eligible_dates')} 日后安全停止。"
                f"请减小 --days-back 或 --chunk-days 后分批续跑。",
                file=sys.stderr,
            )
            return 3

        failed = int(result.get("scope_counts", {}).get("failed", 0))
        if failed > 0:
            print(f"ERROR: 存在 {failed} 个失败 scope，请检查 reason_codes", file=sys.stderr)
            return 3

        return 0


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器（供自测复用）。"""
    parser = argparse.ArgumentParser(
        description="Review Bootstrap CLI - 从 canonical PIT 历史回填 scope observations",
    )
    parser.add_argument(
        "--days-back", type=int, default=120,
        help=f"回溯天数（默认 120，最低 {MIN_DAYS_BACK}）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="只计算不写入（默认 True，零业务写入）",
    )
    parser.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false",
        help="实际写入（需管理员确认）",
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="截止交易日（YYYY-MM-DD；缺省=最近一个完整 A 股交易日）",
    )
    parser.add_argument(
        "--operator", type=str, required=True,
        help="执行人标识（必填，审计用）",
    )
    parser.add_argument(
        "--reason", type=str, required=True,
        help="执行原因（必填，审计用）",
    )
    parser.add_argument(
        "--algorithm-version", type=str, default=None,
        help="显式算法版本（缺省=当前 REVIEW_ALGORITHM_VERSION）",
    )
    parser.add_argument(
        "--summary-only", action="store_true", default=False,
        help="只打印摘要与四类计数，不打印逐日明细",
    )
    parser.add_argument(
        "--chunk-days", type=int, default=DEFAULT_BOOTSTRAP_CHUNK_DAYS,
        help=(
            f"每分片处理的交易日数（默认 {DEFAULT_BOOTSTRAP_CHUNK_DAYS}）；"
            "分片越小峰值内存越低"
        ),
    )
    parser.add_argument(
        "--memory-budget-mb", type=int, default=DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB,
        help=(
            f"RSS 软预算 MB（默认 {DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB}）；"
            "超出后安全停止并返回 memory_budget_exceeded"
        ),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
