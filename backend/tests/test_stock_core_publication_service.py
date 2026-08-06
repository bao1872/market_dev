"""stock_core 原子 publication service 单元测试（P0-07 failure-injection）。

[CHANGE-20260805-CP4A-CP3 / P0-07]
验证（mock DB，不连真实库）：
- quality gate 失败（actual < eligible）→ StockCorePublicationError，不写 pointer。
- fencing 失败（其他 worker 持有 / epoch 更旧）→ 拒绝覆盖。
- 无 supersede 列（Migration 未执行）时退化为 upsert（不炸）。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.stock_core_publication_service import (
    StockCorePublicationError,
    publish_stock_core_atomically,
)


def _make_db(actual_count: int, has_sup: bool = True) -> MagicMock:
    db = MagicMock()

    async def _execute(stmt):
        res = MagicMock()
        # 处理 count 查询与 select
        res.scalar_one.return_value = actual_count
        res.scalar_one_or_none.return_value = None
        return res

    db.execute = _execute

    async def _exec_text(stmt, params=None):
        return MagicMock()

    db.exec_driver_sql = _exec_text
    db.flush = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_publish_quality_gate_failure_raises() -> None:
    """actual < eligible → StockCorePublicationError，不写 pointer。"""
    db = _make_db(actual_count=3, has_sup=True)
    db.get = AsyncMock(return_value=None)

    with pytest.raises(StockCorePublicationError):
        await publish_stock_core_atomically(
            db,
            scope_key="market",
            trade_date=date(2026, 8, 6),
            publication_kind="stock_core",
            algorithm_version="v1",
            snapshot_run_id=uuid.uuid4(),
            coverage_ratio=0.6,
            worker_id="w1",
            lease_epoch=1,
            eligible_count=5,  # actual=3 < 5 → 失败
        )


@pytest.mark.asyncio
async def test_publish_fencing_rejects_foreign_worker(monkeypatch) -> None:
    """当前有效 publication 由其他 worker 持有 → 拒绝覆盖（fencing）。"""
    import app.services.stock_core_publication_service as svc

    db = _make_db(actual_count=10, has_sup=True)
    db.get = AsyncMock(return_value=None)

    foreign = MagicMock()
    foreign.publish_worker_id = "w-other"
    foreign.publish_lease_epoch = 5

    async def _execute(stmt):
        res = MagicMock()
        res.scalar_one.return_value = 10
        # 对 FactorPublication select 返回 foreign（当前有效 pointer）
        res.scalar_one_or_none.return_value = foreign
        return res

    db.execute = _execute
    # 强制认为 Migration 087 已应用（有 supersede/fencing 列），使 fencing 生效
    monkeypatch.setattr(svc, "_has_supersede_columns", lambda db: True)

    with pytest.raises(StockCorePublicationError, match="fencing"):
        await publish_stock_core_atomically(
            db,
            scope_key="market",
            trade_date=date(2026, 8, 6),
            publication_kind="stock_core",
            algorithm_version="v1",
            snapshot_run_id=uuid.uuid4(),
            coverage_ratio=1.0,
            worker_id="w1",
            lease_epoch=2,
            eligible_count=10,
        )


@pytest.mark.asyncio
async def test_publish_fails_closed_without_migration(monkeypatch) -> None:
    """[P0-C] 无 supersede 列（Migration 087 未执行）→ fail-closed，禁止发布。

    不得退回旧的非原子 upsert 路径。
    """
    import app.services.stock_core_publication_service as svc

    db = _make_db(actual_count=10, has_sup=False)
    db.get = AsyncMock(return_value=None)

    # Migration 087 缺失：_has_supersede_columns 返回 False
    monkeypatch.setattr(svc, "_has_supersede_columns", lambda db: False)

    with pytest.raises(StockCorePublicationError, match="NOT_READY"):
        await publish_stock_core_atomically(
            db,
            scope_key="market",
            trade_date=date(2026, 8, 6),
            publication_kind="stock_core",
            algorithm_version="v1",
            snapshot_run_id=uuid.uuid4(),
            coverage_ratio=1.0,
            worker_id="w1",
            lease_epoch=1,
            eligible_count=10,
        )


def _success_db(monkeypatch) -> MagicMock:
    """构造一个成功发布路径的 fake db（Migration 087 已应用）。"""
    import app.services.stock_core_publication_service as svc

    monkeypatch.setattr(svc, "_has_supersede_columns", lambda db: True)
    db = MagicMock()

    async def _execute(stmt):
        res = MagicMock()
        res.scalar_one.return_value = 10  # coverage count
        res.scalar_one_or_none.return_value = None  # 无旧 pointer
        return res

    db.execute = _execute
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_publish_failure_injection_flush_fails(monkeypatch) -> None:
    """[P0-C] publication insert 后 flush 失败 → 抛错，整体回滚（不写 pointer/run）。"""
    db = _success_db(monkeypatch)

    async def _boom():
        raise RuntimeError("flush failed")

    db.flush = _boom
    with pytest.raises(RuntimeError, match="flush failed"):
        await publish_stock_core_atomically(
            db,
            scope_key="market",
            trade_date=date(2026, 8, 6),
            publication_kind="stock_core",
            algorithm_version="v1",
            snapshot_run_id=uuid.uuid4(),
            coverage_ratio=1.0,
            worker_id="w1",
            lease_epoch=1,
            eligible_count=10,
        )
    # 未到达 run mark（flush 失败提前抛）→ run 未被标 published
    assert db.get.await_count == 0


def test_orchestrator_wires_atomic_publication_service() -> None:
    """[P0-C] 正式 orchestrator 必须调用 publish_stock_core_atomically，且 scheduled 路径
    不再调用旧的 two-phase publish_stock_core。"""
    from pathlib import Path

    _base = Path(__file__).resolve().parents[1]
    src = (_base / "app/services/after_close_orchestrator.py").read_text(encoding="utf-8")
    assert "publish_stock_core_atomically" in src, (
        "orchestrator 应调用 publish_stock_core_atomically"
    )
    # 旧 two-phase publish_stock_core 不应再被 scheduled 发布路径调用
    assert "publish_stock_core(" not in src, (
        "orchestrator 不应再调用旧 publish_stock_core（two-phase）"
    )
    # [P0-C] 正常链只保留一个 run 状态 owner（原子发布服务）；不应再有内联 phase-2 run-mark
    # （publishing 步骤内独立调 finish_snapshot_run 标记 succeeded）
    assert "Only mark snapshot succeeded" not in src, (
        "正常链不应再有独立的 phase-2 run-mark 注释/逻辑"
    )


@pytest.mark.asyncio
async def test_publish_failure_injection_audit_fails(monkeypatch) -> None:
    """[P0-C] audit insert 失败 → 抛错（同事务，旧 pointer/run 不漂移）。"""
    db = _success_db(monkeypatch)

    async def _execute(stmt):
        res = MagicMock()
        res.scalar_one.return_value = 10
        res.scalar_one_or_none.return_value = None
        return res

    db.execute = _execute

    async def _audit_boom(stmt, params=None):
        if "stock_core_publication_audit" in str(stmt):
            raise RuntimeError("audit insert failed")
        res = MagicMock()
        res.scalar_one.return_value = 10
        res.scalar_one_or_none.return_value = None
        return res

    db.execute = _audit_boom
    db.flush = AsyncMock()
    with pytest.raises(RuntimeError, match="audit insert failed"):
        await publish_stock_core_atomically(
            db,
            scope_key="market",
            trade_date=date(2026, 8, 6),
            publication_kind="stock_core",
            algorithm_version="v1",
            snapshot_run_id=uuid.uuid4(),
            coverage_ratio=1.0,
            worker_id="w1",
            lease_epoch=1,
            eligible_count=10,
        )


@pytest.mark.asyncio
async def test_reconcile_finalizes_stale_running_and_supersedes_duplicates(
    monkeypatch,
) -> None:
    """[CP4A.2 Step4] reconcile：历史分裂 pointer→supersede 保留最新；stale running run→succeeded。

    正常 scheduled 不调用 reconcile（只在部署中断/历史遗留后手动触发）。
    """
    import app.services.stock_core_publication_service as svc

    monkeypatch.setattr(svc, "_has_supersede_columns", lambda db: True)

    run_id_old, run_id_new = uuid.uuid4(), uuid.uuid4()
    pointer_old, pointer_new = MagicMock(), MagicMock()
    pointer_old.data_run_id = run_id_old
    pointer_old.id = uuid.uuid4()
    pointer_old.published_at = date(2026, 8, 5)
    pointer_new.data_run_id = run_id_new
    pointer_new.id = uuid.uuid4()
    pointer_new.published_at = date(2026, 8, 6)

    # 两个当前有效 pointer（旧/新）+ 一个 stale running run（新 pointer 对应）
    pointers = [pointer_new, pointer_old]
    stale_run = MagicMock()
    stale_run.status = "running"
    stale_run.published_at = None

    db = AsyncMock()
    db.get = AsyncMock(return_value=stale_run)

    async def _execute(stmt):
        res = MagicMock()
        # pointers select 返回 [new, old]（按 published_at 降序）
        res.scalars.return_value.all.return_value = pointers
        return res

    db.execute = _execute
    db.flush = AsyncMock()

    result = await svc.reconcile_stock_core_publication(
        db, trade_date=date(2026, 8, 6),
    )

    # 保留新 pointer，supersede 旧 pointer（历史分裂修复）
    assert result["superseded_duplicates"] == 1
    assert pointer_old.superseded_by == pointer_new.id
    # stale running run → succeeded（pointer/run 分裂修复）
    assert result["run_finalized"] == 1
    assert stale_run.status == "succeeded"
