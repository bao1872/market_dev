#!/usr/bin/env python3
"""V2.1 验证数据 seed CLI（DS-112）。

从 bz_stock（只读）复制有限数据到验证库 bz_stock_verify_<sha>，生成四类代表状态：
  A 完整成功 / B 异步增强 / C 降级 / D 治理与恢复。

约束（DS-112）：
  - 不完整复制 bz_stock（只复制有限 instrument/bars/boards，约 30-50 只、90-120 交易日）。
  - 对 bz_stock 只读（SELECT），绝不写入。
  - 可重跑（幂等）：重建验证库时再次运行不冲突。
  - 不得写成一次性远程脚本（本文件受版本控制）。

注意：本 CLI 的真实数据生成依赖验证库 schema（Migration 085/086 apply 后）。
Phase 3 创建骨架；Phase 4 在验证库首次运行时补全四类场景的具体数据写入逻辑。

用法：
  python scripts/verify/seed_v21_verify_data.py \
      --verify-db-url postgresql+psycopg://bz:***@trading-postgres:5432/bz_stock_verify_<sha> \
      --biz-db-url postgresql+psycopg://bz:***@trading-postgres:5432/bz_stock \
      --scenario all
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{7,40}$")


async def _connect_verify(verify_db_url: str) -> str:
    """连接校验：current_database() 必须 == 验证库，且 != bz_stock。"""
    try:
        import asyncpg
    except ImportError:
        print("seed: 需要 asyncpg（远程验证环境已安装）", file=sys.stderr)
        raise
    db_name = verify_db_url.rsplit("/", 1)[-1]
    if not VERIFY_DB_RE.match(db_name):
        raise RuntimeError(f"非法验证库名 '{db_name}'（必须 bz_stock_verify_<sha>）")
    conn = await asyncpg.connect(verify_db_url.replace("postgresql+psycopg://", "postgresql://"))
    cur = await conn.fetchval("SELECT current_database()")
    if cur != db_name:
        await conn.close()
        raise RuntimeError(f"连接校验失败 current_database='{cur}' 期望 '{db_name}'")
    if cur == "bz_stock":
        await conn.close()
        raise RuntimeError("严重错误：连接到了 bz_stock，立即中止")
    return cur


async def _read_biz_readonly(biz_db_url: str, query: str, *args):
    """对 bz_stock 只读查询（SELECT），绝不写入。"""
    import asyncpg

    conn = await asyncpg.connect(biz_db_url.replace("postgresql+psycopg://", "postgresql://"))
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


# 四类场景：标记哪些 instrument/交易日用于哪类验证。
SCENARIO_A_FULL_SUCCESS = "full_success"
SCENARIO_B_ASYNC_ENHANCE = "async_enhance"
SCENARIO_C_DEGRADED = "degraded"
SCENARIO_D_GOVERNANCE = "governance"

SCENARIOS = {
    SCENARIO_A_FULL_SUCCESS: "stock_core ready / dsa ready / state_events ready / chip ready / auction composite / review ready → fully_ready",
    SCENARIO_B_ASYNC_ENHANCE: "stock_core ready / review ready / chip running / auction structure_only → core_ready",
    SCENARIO_C_DEGRADED: "board_facts reused / chip partial / auction hybrid → degraded_ready",
    SCENARIO_D_GOVERNANCE: "publication missing / lease lost / retryable child / granular restart / reconcile",
}


async def seed_scenario(verify_db_url: str, biz_db_url: str, scenario: str) -> None:
    """为指定场景生成有限验证数据。

    Phase 4 补全：从 bz_stock 只读拉取对应 instrument/bars/boards，幂等写入验证库，
    并写入场景标记（便于验收 Runbook 识别四类状态）。
    """
    db = await _connect_verify(verify_db_url)
    print(f"seed: 场景 {scenario} 目标库={db}（DS-112 框架已就位，Phase 4 补全数据写入）")
    # TODO(Phase 4): 真实四类场景数据生成逻辑（依赖验证库 Migration 后的 schema）。
    # 此处仅连接校验 + 场景登记，避免伪造成功数据。


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-db-url", required=True)
    ap.add_argument("--biz-db-url", required=True)
    ap.add_argument("--scenario", choices=["all", *SCENARIOS.keys()], default="all")
    args = ap.parse_args()

    if args.scenario == "all":
        for sc in SCENARIOS:
            await seed_scenario(args.verify_db_url, args.biz_db_url, sc)
    else:
        await seed_scenario(args.verify_db_url, args.biz_db_url, args.scenario)

    print("seed: 完成（框架）。Phase 4 在验证库执行真实数据生成。")


if __name__ == "__main__":
    asyncio.run(main())
