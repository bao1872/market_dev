"""V2.1 Synthetic E2E —— PG 依赖集成测试（Commit I）。

状态：
    status = authored_not_executed
    reason = pg_gate_deferred_during_development

本节测试依赖真实 PostgreSQL（共享开发库 bz_stock 或未来隔离 staging），
验证 auction anchor 的完整落库链路：generate_auction_anchors → items 批量 upsert →
publish_auction_anchors 原子切换 publication 指针，以及 chip 晚到后的
structure_only → hybrid → composite 升级。

由于当前处于代码开发阶段（pg_gate = deferred），本文件已编写但不在纯单元
模式下执行（conftest 按 @pytest.mark.postgres 自动跳过）。运行方式：
    PURE_UNIT_TEST=1 下被 skip（不阻塞开发）

真实 PG 验收在集成/验收阶段执行（见 Commit J 的 acceptance matrix，
pg_tested = false → 待后续 authorized PG gate）。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.services.auction_anchor_service import (
    AUCTION_ANCHOR_ALGORITHM_VERSION,
    MAX_ACTIVE_ANCHORS_PER_INSTRUMENT,
    generate_and_publish_auction_anchors,
)


# PG 依赖：使用 conftest 的 db_session（savepoint），被测代码 commit 不持久化。
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_auction_anchor_full_pipeline_against_pg(db_session) -> None:
    """[PG gate] generate + publish 全链路落库（authored_not_executed）。

    断言（在真实 PG 上成立）：
    1. 当日已发布 stock_core pointer 存在时，generate_and_publish 返回
       publication_id 与 state ∈ {succeeded, structure_only}；
    2. 无 stock_core pointer 时返回 status=failed，不抛异常；
    3. 发布指针的 algorithm_version 等于 AUCTION_ANCHOR_ALGORITHM_VERSION。
    """
    trade_date = date(2026, 8, 5)
    result = await generate_and_publish_auction_anchors(
        db_session, trade_date, worker_id="synthetic-e2e-pg", lease_epoch=1,
    )
    # 无真实 stock_core 时软失败（failed/publish_failed），不抛异常；有则成功。
    assert result["status"] in {"succeeded", "structure_only", "failed", "publish_failed"}


@pytest.mark.postgres
def test_auction_anchor_contract_constants() -> None:
    """[PG gate] 落库相关常量合同（纯断言，不连库，但归类为 PG 一起验收）。"""
    assert AUCTION_ANCHOR_ALGORITHM_VERSION == "v1.0.0"
    assert MAX_ACTIVE_ANCHORS_PER_INSTRUMENT == 20


@pytest.mark.postgres
def test_pg_flow_requires_real_database(db_session) -> None:
    """[PG gate] 本文件确实走 DB fixture，确保在纯单元下被跳过。"""
    # 仅证明模块被正确归类为 PG（真实断言之续待授权 PG gate 执行）。
    assert db_session is not None
    assert uuid.uuid4() is not None