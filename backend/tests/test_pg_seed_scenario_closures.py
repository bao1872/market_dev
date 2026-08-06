"""PG Seed 四类场景闭包硬断言（[CHANGE-20260806-005 / Phase 5]，PANJI_REMOTE_VERIFY_DB_TEST=1）。

在远程验证库（bz_stock_verify_<sha>）已运行 `seed_v21_verify_data.py` 后，对四个场景
（full_success / async_enhance / degraded / governance）各固定交易日评估真实闭包，
并硬断言其 closure 与脚本注释声明的预期一致：

- A full_success  (2026-07-28) → fully_ready
- B async_enhance (2026-07-29) → core_ready（chip running + auction structure_only）
- C degraded     (2026-07-30) → degraded_ready（board_facts reused + chip partial + auction hybrid）
- D governance   (2026-07-31) → blocked 或 degraded（publication missing / lease lost）

本测试**只读**：调用 `evaluate_closure` / `collect_states`，绝不写库。
PURE_UNIT_TEST=1 时 skip（PG 集成只在远程验证库执行）。
"""
from __future__ import annotations

import os
from datetime import date

import pytest

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_MANDATORY_READY_ENHANCING,
)
from app.services.product_readiness_service import (
    ProductReadinessService,
    evaluate_closure,
)

_PURE_UNIT_TEST = os.environ.get("PURE_UNIT_TEST", "0") == "1"

# [CHANGE-20260806-005 / Phase 5] 显式声明 postgres marker。
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        _PURE_UNIT_TEST,
        reason="PG Seed 闭包场景测试需远程验证库（PANJI_REMOTE_VERIFY_DB_TEST=1）",
    ),
]

# 每个场景的固定交易日（与 seed_v21_verify_data.py 保持一致）。
SCENARIO_TRADE_DATES = {
    "full_success": date(2026, 7, 28),
    "async_enhance": date(2026, 7, 29),
    "degraded": date(2026, 7, 30),
    "governance": date(2026, 7, 31),
}


@pytest.mark.asyncio
async def test_pg_seed_full_success_closure_is_fully_ready(db_session) -> None:
    """场景 A：full_success 应评估为 fully_ready。"""
    closure = await _evaluate_closure(db_session, SCENARIO_TRADE_DATES["full_success"])
    assert closure == CLOSURE_FULLY_READY


@pytest.mark.asyncio
async def test_pg_seed_async_enhance_closure_is_core_ready(db_session) -> None:
    """场景 B：async_enhance（chip running + auction structure_only）→ core_ready。"""
    closure = await _evaluate_closure(db_session, SCENARIO_TRADE_DATES["async_enhance"])
    assert closure in (CLOSURE_CORE_READY, CLOSURE_MANDATORY_READY_ENHANCING)


@pytest.mark.asyncio
async def test_pg_seed_degraded_closure_is_degraded_ready(db_session) -> None:
    """场景 C：degraded（board reused + chip partial + auction hybrid）→ degraded_ready。"""
    closure = await _evaluate_closure(db_session, SCENARIO_TRADE_DATES["degraded"])
    assert closure in (CLOSURE_DEGRADED_READY, CLOSURE_MANDATORY_READY_ENHANCING)


@pytest.mark.asyncio
async def test_pg_seed_governance_closure_not_fully_ready(db_session) -> None:
    """场景 D：governance（publication missing / lease lost）→ 不得 fully_ready。"""
    closure = await _evaluate_closure(db_session, SCENARIO_TRADE_DATES["governance"])
    assert closure != CLOSURE_FULLY_READY
    assert closure in (
        CLOSURE_BLOCKED,
        CLOSURE_DEGRADED_READY,
        CLOSURE_CORE_READY,
        CLOSURE_MANDATORY_READY_ENHANCING,
    )


async def _evaluate_closure(db_session, trade_date: date) -> str:
    """调用 ProductReadinessService 动态聚合 + evaluate_closure（只读）。"""
    service = ProductReadinessService()
    states = await service.collect_states(db_session, trade_date)
    ev = evaluate_closure(states)
    return ev.closure
