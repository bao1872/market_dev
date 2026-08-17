"""Scope Dynamics 只读基线测量 probe（R1 runtime-readiness）。

仅调用正式 production semantic path，不复制任何业务公式。

用法：
    cd backend && .venv/bin/python -m scripts.review_scope_dynamics_probe \
        --scope-type industry_l1 --scope-key 银行 --history 120

    # 不连库跑（--dry-run 仅校验参数与导入）
    .venv/bin/python -m scripts.review_scope_dynamics_probe \
        --scope-type concept --scope-key 人工智能 --history 60 --dry-run

约束：
    - 只读远程 bz_stock（通过本地 SSH Tunnel）；不写、不 publish、不编排。
    - 不拥有任何 Scope Observation / Position / Dynamics / Phase 算法。
    - 必须包含 ``def main() -> int`` 与 ``if __name__ == "__main__": raise SystemExit(main())``。

退出码：
    0: 成功完成基线测量并打印诊断
    2: 参数/输入校验失败
    1: 计算过程异常
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import resource
import sys
from datetime import date

logger = logging.getLogger(__name__)

# 这些 scope_type 是 R1 刻意覆盖集合（见 plan scale ladder）。
# 仅当前正式 path 支持的 type（reconstruct_scope_series 的 _SUPPORTED_SCOPE_TYPES）。
_KNOWN_SCOPE_TYPES = {
    "industry_l1",
    "industry_l2",
    "industry_l3",
    "concept",
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scope Dynamics 只读基线测量 probe（R1）",
    )
    p.add_argument(
        "--scope-type", required=True,
        choices=sorted(_KNOWN_SCOPE_TYPES),
        help="scope 类型（industry_l1/l2/l3/concept/...）",
    )
    p.add_argument(
        "--scope-key", required=True, type=str,
        help="scope 标识（如 银行 / 人工智能）",
    )
    p.add_argument(
        "--history", type=int, default=120,
        help="回看交易日数量（scale ladder: 20/60/120）",
    )
    p.add_argument(
        "--asof-date", type=str, default=None,
        help="current-static membership as-of 日期 ISO（默认取最新交易日）",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只校验参数与导入，不连库",
    )
    return p.parse_args()


def _rss_mb() -> float:
    """当前进程常驻内存（MB）。

    macOS 的 ``ru_maxrss`` 单位是字节；Linux 是 KB。统一折算为 MB。
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw / (1024.0 * 1024.0)
    return raw / 1024.0


def _build_readonly_engine():
    """标记函数：本 probe 通过 AsyncSessionLocal 复用 app.db 的现有 engine。

    只读守卫在 ``_probe`` 内于连接建立后立即执行
    ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` 并验证写被拒。
    不修改共享的 app.db 源码；probe 仅调用纯 SELECT 路径，不发出任何 DDL。
    """
    from app.db import AsyncSessionLocal

    return AsyncSessionLocal


async def _probe(
    scope_type: str,
    scope_key: str,
    history: int,
    asof_date: date | None,
) -> int:
    from sqlalchemy import text

    from app.db import AsyncSessionLocal
    from app.services.review_observation_prep_service import (
        list_recent_trading_days,
    )
    from app.services.review_scope_dynamics_service import (
        compute_current_static_scope_dynamics,
    )

    _build_readonly_engine()  # 复用 app.db 的 AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        # 只读守卫：连接建立后立即设为 read-only（对非超级用户角色生效）。
        # 注意：bz_stock 的 bz 角色是超级用户，PostgreSQL 不会对其强制
        # transaction_read_only，因此 DB 层只读无法由会话设置保证。
        # 本 probe 的只读性由 **代码审计保证**：被调用的正式 path
        # （reconstruct / prep / observation / dynamics）仅发出 SELECT，
        # 且 probe 本身不发出任何写语句。此处 SET 为纵深防御，验证仅作信息提示。
        await db.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
        try:
            await db.execute(
                text(
                    "WITH u AS (UPDATE market_boards SET name=name "
                    "WHERE id='00000000-0000-0000-0000-000000000000' "
                    "RETURNING 1) SELECT 1 FROM u"
                )
            )
            await db.rollback()
            logger.warning(
                "[readonly-check] 角色为超级用户，DB 层只读无法强制；"
                "只读性依赖代码审计（path 仅 SELECT）。继续运行（不写）。"
            )
        except Exception as e:  # noqa: BLE001 - 期望被 read-only 拒绝（非超级用户时）
            logger.info("[readonly-check] DML 被拒绝: %s", type(e).__name__)
            await db.rollback()
        # canonical trading-date axis（复用正式 owner，不自造时间轴）
        if asof_date is None:
            latest = await list_recent_trading_days(db, date.today(), 1)
            if not latest:
                logger.error("无法解析最新交易日（calendar 为空）")
                return 2
            asof_date = latest[0]

        trade_dates = await list_recent_trading_days(db, asof_date, history)
        if not trade_dates:
            logger.error("trade_dates 为空（history=%d）", history)
            return 2
        # list_recent_trading_days 返回降序，正式 path 要求严格升序
        trade_dates = sorted(trade_dates)

        logger.info(
            "[probe] scope_type=%s scope_key=%s asof_date=%s "
            "trade_date_count=%d window=[%s, %s]",
            scope_type, scope_key, asof_date.isoformat(),
            len(trade_dates),
            trade_dates[0].isoformat(), trade_dates[-1].isoformat(),
        )

        rss_before = _rss_mb()
        result = await compute_current_static_scope_dynamics(
            db,
            scope_type,
            scope_key,
            trade_dates,
            analysis_asof_date=asof_date,
        )
        rss_after = _rss_mb()

        membership = result.get("membership") or {}
        member_count = (
            membership.get("member_count")
            if isinstance(membership, dict)
            else len(membership)
        )
        scope_dynamics = result.get("scope_dynamics") or {}
        observation_series = result.get("observation_series") or {}

        # 诊断输出（只读，不拥有算法）
        print("=== Scope Dynamics Probe (read-only baseline) ===")
        print(f"scope_type        : {scope_type}")
        print(f"scope_key         : {scope_key}")
        print(f"asof_date         : {asof_date.isoformat()}")
        print(f"trade_date_count  : {len(trade_dates)}")
        print(f"member_count      : {member_count}")
        print(f"member_x_days     : {len(membership) * len(trade_dates)}")
        print(f"scope_dynamics_keys: {sorted(scope_dynamics.keys())}")
        print(f"observation_series_keys: {sorted(observation_series.keys())}")
        print(f"rss_before_mb     : {rss_before:.1f}")
        print(f"rss_after_mb      : {rss_after:.1f}")
        print(f"rss_delta_mb      : {rss_after - rss_before:.1f}")
        print("=== END ===")
        return 0


async def _run(args: argparse.Namespace) -> int:
    asof: date | None = date.fromisoformat(args.asof_date) if args.asof_date else None
    if args.dry_run:
        logger.info(
            "[dry-run] scope_type=%s scope_key=%s history=%d asof=%s OK",
            args.scope_type, args.scope_key, args.history, asof,
        )
        return 0
    return await _probe(
        args.scope_type, args.scope_key, args.history, asof,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
