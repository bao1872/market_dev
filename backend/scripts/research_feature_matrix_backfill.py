"""研究特征矩阵回补脚本骨架。

目标：为研究矩阵建立因果口径骨架，不接入生产 snapshot，不修改 watchlist_ready。

与生产 stock_feature_snapshots 的区别：
- production snapshot: 服务最近交易日 + 自选股 + 前端展示，必须 point-in-time
- research matrix: 探索因子组合规律，可同时包含 causal/hindsight/label，但严格分命名空间

本 PR 只完成骨架 + dry-run：
- dry-run 只打印计划和字段分类统计，不写 DB，不写文件
- 非 dry-run 无 --output 也只打印计划（骨架阶段不实际计算）
- --output 必须配合 sample scope（--symbols 或 --limit-instruments）
- 禁止无过滤全市场输出文件

后续实现顺序（不在本 PR 范围）：
1. causal rolling features (ATR/BB/SQZMOM/volume)
2. confirmed_delay swing（按确认 bar 生效，不回填 anchor）
3. DSA 双轨（causal.dsa_confirmed_* / hindsight.dsa_finalized_*）
4. Node Cluster（只输出 hindsight.node_cluster_*）
5. labels（用未来 close/high/low 生成 label.future_*）

用法：
    # dry-run 查看计划
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --start 2026-01-01 --end 2026-01-31 --symbols 000001 --dry-run

    # 指定输出（必须 sample scope）
    cd backend && python -m scripts.research_feature_matrix_backfill \\
        --start 2026-01-01 --limit-instruments 100 \\
        --output /tmp/research.parquet --dry-run

约束：
- 不新增数据库表
- 不写大 CSV/parquet（骨架阶段不写任何文件）
- 不接入 watchlist_ready
- 不修改 production snapshot
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

from app.research.feature_causality_registry import (
    NS_CAUSAL,
    NS_CONFIRMED_DELAY,
    NS_HINDSIGHT,
    NS_LABEL,
    build_default_registry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("research_feature_matrix_backfill")


def parse_args() -> argparse.Namespace:
    """解析 CLI 参数。

    Returns:
        argparse.Namespace，包含 start/end/symbols/limit_instruments/dry_run/
        output/include_hindsight/include_labels
    """
    parser = argparse.ArgumentParser(
        description="研究特征矩阵回补骨架（因果口径 registry + dry-run 计划）",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="起始日期（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--end",
        default="latest",
        help="结束日期（YYYY-MM-DD 或 'latest'，默认 latest）",
    )
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与字段分类统计，不写 DB，不写文件",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出文件路径（可选，必须配合 sample scope）",
    )
    parser.add_argument(
        "--include-hindsight",
        type=str,
        default="true",
        choices=["true", "false"],
        help="是否包含 hindsight 命名空间字段（默认 true）",
    )
    parser.add_argument(
        "--include-labels",
        type=str,
        default="true",
        choices=["true", "false"],
        help="是否包含 label 命名空间字段（默认 true）",
    )
    args = parser.parse_args()

    # 解析 --symbols 为 list
    if args.symbols:
        args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    # 解析 --include-hindsight / --include-labels 为 bool
    args.include_hindsight = args.include_hindsight == "true"
    args.include_labels = args.include_labels == "true"

    return args


def _resolve_scope(
    symbols: list[str] | None,
    limit_instruments: int | None,
) -> str:
    """根据过滤条件决定 scope。

    - 有 --symbols 或 --limit-instruments → 'sample'
    - 都无 → 'full'
    """
    if symbols and len(symbols) > 0:
        return "sample"
    if limit_instruments is not None and limit_instruments > 0:
        return "sample"
    return "full"


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    """构建研究矩阵计划，包含字段分类统计。

    根据 --include-hindsight / --include-labels 开关统计各命名空间字段数。
    不实际查询 DB（骨架阶段），只基于 registry 计数。

    Args:
        args: parse_args 返回的 Namespace

    Returns:
        dict 包含:
        - start, end: 日期范围
        - scope: 'sample' / 'full'
        - field_classification: {namespace: count}
        - total_fields: 启用字段总数
    """
    reg = build_default_registry()

    # 根据开关统计字段分类
    fc: dict[str, int] = {
        NS_CAUSAL: len(reg.by_namespace(NS_CAUSAL)),
        NS_CONFIRMED_DELAY: len(reg.by_namespace(NS_CONFIRMED_DELAY)),
        NS_HINDSIGHT: len(reg.by_namespace(NS_HINDSIGHT))
        if args.include_hindsight
        else 0,
        NS_LABEL: len(reg.by_namespace(NS_LABEL))
        if args.include_labels
        else 0,
    }

    scope = _resolve_scope(args.symbols, args.limit_instruments)

    return {
        "start": args.start,
        "end": args.end,
        "scope": scope,
        "symbols": args.symbols,
        "limit_instruments": args.limit_instruments,
        "field_classification": fc,
        "total_fields": sum(fc.values()),
        "include_hindsight": args.include_hindsight,
        "include_labels": args.include_labels,
    }


def _validate_output_scope(args: argparse.Namespace) -> None:
    """校验 --output 必须配合 sample scope。

    --output 指定时必须有 --symbols 或 --limit-instruments，
    禁止无过滤全市场输出文件。
    """
    if args.output is None:
        return
    scope = _resolve_scope(args.symbols, args.limit_instruments)
    if scope != "sample":
        raise ValueError(
            f"--output 必须配合 sample scope（--symbols 或 --limit-instruments），"
            f"当前 scope={scope}，禁止无过滤全市场输出文件"
        )


def _print_plan(plan: dict[str, Any]) -> None:
    """打印研究矩阵计划到 stdout。"""
    fc = plan["field_classification"]
    print("=" * 60)
    print("[research_feature_matrix] 研究矩阵计划")
    print("=" * 60)
    print(f"start: {plan['start']}")
    print(f"end: {plan['end']}")
    print(f"scope: {plan['scope']}")
    if plan.get("symbols"):
        print(f"symbols: {plan['symbols']}")
    if plan.get("limit_instruments"):
        print(f"limit_instruments: {plan['limit_instruments']}")
    print(f"include_hindsight: {plan['include_hindsight']}")
    print(f"include_labels: {plan['include_labels']}")
    print("-" * 60)
    print("字段分类统计:")
    print(f"  causal:           {fc[NS_CAUSAL]}")
    print(f"  confirmed_delay:  {fc[NS_CONFIRMED_DELAY]}")
    print(f"  hindsight:        {fc[NS_HINDSIGHT]}")
    print(f"  label:            {fc[NS_LABEL]}")
    print(f"  total_fields:     {plan['total_fields']}")
    print("-" * 60)
    print("命名空间说明:")
    print("  causal:           当时可知的滚动特征（允许回测）")
    print("  confirmed_delay:  确认 bar 生效字段（允许回测，不回填 anchor）")
    print("  hindsight:        允许未来信息（禁止回测，只做结构标注）")
    print("  label:            未来标签（禁止作为 feature，只能作为 y）")
    print("=" * 60)


def main() -> None:
    """主入口：解析参数 -> 校验 scope -> 构建计划 -> 打印。

    骨架阶段行为：
    - dry-run: 打印计划 + 字段分类，不写 DB，不写文件
    - 非 dry-run 无 --output: 打印计划（骨架阶段不实际计算）
    - --output: 校验 sample scope（dry-run 时不写文件）
    """
    args = parse_args()

    # 校验 --output scope（即使 dry-run 也要校验）
    _validate_output_scope(args)

    plan = build_plan(args)

    # 打印计划（dry-run 和非 dry-run 都打印）
    _print_plan(plan)

    if args.dry_run:
        print("[dry-run] 不写 DB，不写文件，只打印计划")
        return

    if args.output:
        # 骨架阶段：不实际写文件
        print(
            f"[skeleton] 计划写入 {args.output}（骨架阶段未实现实际计算）"
        )
    else:
        print("[skeleton] 无 --output，只打印计划（骨架阶段不实际计算）")


if __name__ == "__main__":
    main()
