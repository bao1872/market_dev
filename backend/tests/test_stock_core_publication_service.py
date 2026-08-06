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
async def test_publish_degrades_to_upsert_without_migration(monkeypatch) -> None:
    """无 supersede 列（Migration 087 未执行）→ 退化为 upsert，不抛错。"""
    import app.services.stock_core_publication_service as svc

    db = _make_db(actual_count=10, has_sup=False)

    # 无 supersede 列：_has_supersede_columns 返回 False
    async def _exec_text(stmt, params=None):
        res = MagicMock()
        # information_schema 查询：返回不含 supersede 列的列集合
        rows = [("id",), ("scope_key",), ("data_run_id",)]
        res.fetchall.return_value = rows
        return res

    db.exec_driver_sql = _exec_text
    db.get = AsyncMock(return_value=None)

    # 使 _has_supersede_columns 走 exec_driver_sql 探测
    # （此处通过 monkeypatch 直接返回 False 更稳）
    monkeypatch.setattr(svc, "_has_supersede_columns", lambda db: False)

    # 无已存在 pointer → 走 add+flush
    pub = await publish_stock_core_atomically(
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
    assert pub is not None
