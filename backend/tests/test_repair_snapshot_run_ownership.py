"""repair_snapshot_run_ownership 工具测试。

用户要求 9 项测试中的 2 项：
6. dry-run 不写库
7. execute 正确且幂等

测试策略：
    直接测试 _find_null_snapshots / _find_candidate_runs / _apply_repair 核心函数，
    使用 conftest 的 db_session（savepoint 模式），避免 AsyncSessionLocal 的独立 session 问题。
"""

from __future__ import annotations

import os
import sys
from datetime import date

# 将项目根目录加入 sys.path 以便导入 tools.repair_snapshot_run_ownership
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.models.instrument import Instrument  # noqa: E402
from tests.test_stock_state_and_events import _create_db_run, _create_db_snapshot  # noqa: E402


@pytest_asyncio.fixture
async def repair_test_setup(db_session: AsyncSession):
    """创建测试数据：1 个 instrument + 1 个 published full run + 1 个 NULL source_run_id 快照。"""
    inst = Instrument(symbol="REPAIR01", name="修复测试", market="SZ", status="active")
    db_session.add(inst)
    await db_session.flush()

    run = await _create_db_run(db_session, trade_date=date(2026, 7, 10))
    # 创建快照但不设 source_run_id（模拟 legacy 未关联）
    snap = await _create_db_snapshot(db_session, inst.id, run, source_run_id=None)

    return inst, run, snap


@pytest.mark.asyncio
async def test_repair_dry_run_does_not_write(
    db_session: AsyncSession,
    repair_test_setup,
) -> None:
    """测试 6: dry-run 只查询不写库。

    调用 _find_null_snapshots 和 _find_candidate_runs（dry-run 等价操作），
    验证快照的 source_run_id 仍为 NULL。
    """
    from tools.repair_snapshot_run_ownership import _find_candidate_runs, _find_null_snapshots

    inst, run, snap = repair_test_setup

    # dry-run 等价：只查询
    null_snapshots = await _find_null_snapshots(db_session)
    assert len(null_snapshots) >= 1
    # 确认我们的测试快照在其中
    test_snap = next(s for s in null_snapshots if s.id == snap.id)
    assert test_snap.source_run_id is None

    # 查找候选 run
    candidates = await _find_candidate_runs(db_session, snap)
    assert len(candidates) == 1, "应有唯一候选 run"
    assert candidates[0].id == run.id

    # 验证 source_run_id 仍为 NULL（dry-run 不写）
    await db_session.refresh(snap)
    assert snap.source_run_id is None, "dry-run 不得修改 source_run_id"


@pytest.mark.asyncio
async def test_repair_apply_writes_and_idempotent(
    db_session: AsyncSession,
    repair_test_setup,
) -> None:
    """测试 7: apply 正确写入且幂等（二次 apply 不重复写入）。

    第一次 _apply_repair → source_run_id 被写入。
    第二次 _apply_repair → 0 行受影响（WHERE source_run_id IS NULL 过滤）。
    """
    from tools.repair_snapshot_run_ownership import _apply_repair, _find_null_snapshots

    inst, run, snap = repair_test_setup

    # 第一次 apply
    updated = await _apply_repair(db_session, [snap.id], run.id)
    await db_session.flush()
    assert updated == 1, "第一次 apply 应写入 1 行"

    # 验证 source_run_id 已写入
    await db_session.refresh(snap)
    assert snap.source_run_id == run.id, "source_run_id 应被写入为 run.id"

    # 第二次 apply（幂等性验证）
    # 此时 source_run_id 已非 NULL，_apply_repair 的 WHERE source_run_id IS NULL 应过滤掉
    updated_again = await _apply_repair(db_session, [snap.id], run.id)
    await db_session.flush()
    assert updated_again == 0, "第二次 apply 应 0 行（幂等，WHERE source_run_id IS NULL）"

    # 验证 source_run_id 未被重复修改
    await db_session.refresh(snap)
    assert snap.source_run_id == run.id, "幂等后 source_run_id 不变"

    # 全局验证：_find_null_snapshots 不再返回此快照
    null_after = await _find_null_snapshots(db_session)
    assert snap.id not in {s.id for s in null_after}, "修复后不应出现在 NULL 列表中"
