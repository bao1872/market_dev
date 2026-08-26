"""Slice 1 (REVIEW-CURRENT-OWNER-01) POSTGRES tests — current facts locked to Core run.

[CHANGE-20260826-001 Slice 1] Review(T) current First Pyramid facts come ONLY from the
published StockFeatureSnapshot(T) locked to source_core_run_id.

真实 PostgreSQL 验证（远程 verify 库 bz_stock_verify_<sha>）：
- 断言 current_database() == bz_stock_verify_<sha> 且 != bz_stock（KPI-6）；
- 不自行创建 SQLite engine（旧实现用 sqlite+aiosqlite 自创建，违反仓库测试规则，
  本次已改为复用 conftest 的 TestAsyncSessionLocal，由 PANJI_REMOTE_VERIFY_DB_TEST
  指向 verify 库）。

运行：
    PANJI_REMOTE_VERIFY_DB_TEST=1 .venv/bin/python -m pytest \
        tests/test_slice1_current_facts_lock.py -p no:cacheprovider
（需经 panji-verify 注册的 targeted-pg plan，先跑 Migration 再跑本测试）
"""
import datetime
import uuid

import pytest
from sqlalchemy import text

from tests.conftest import TestAsyncSessionLocal
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.services.observation_prep import _BOARD_CURRENT_FLAT_KEY
from app.services.review_observation_prep_service import (
    _load_current_only_snapshot_facts,
)

pytestmark = pytest.mark.postgres


async def _seed_instrument(s, iid: uuid.UUID, symbol: str):
    """verify DB 是空迁移库，无 instruments 种子；snapshot 有 FK 约束，必须自建行。"""
    s.add(Instrument(
        id=iid, symbol=symbol, name=f"slice1_{symbol}", market="SZ",
        status="active", listing_date=datetime.date(2010, 1, 4),
    ))
    await s.flush()


async def _assert_verify_db_identity():
    """KPI-6: 必须连在 verify 库，绝不连 bz_stock。"""
    async with TestAsyncSessionLocal() as s:
        db_name = (await s.execute(text("SELECT current_database()"))).scalar()
    assert db_name is not None
    assert db_name.startswith("bz_stock_verify_"), (
        f"PG 测试必须连 verify 库，实际={db_name}"
    )
    assert db_name != "bz_stock", "禁止连生产 bz_stock"


async def test_verify_db_identity():
    await _assert_verify_db_identity()


async def test_current_facts_locked_to_source_core_run_id():
    """KPI-3: same-day 两 run，Review 只消费 source_core_run_id 的快照；
    错误 run 被忽略（无 fallback）。"""
    await _assert_verify_db_identity()

    iid = uuid.UUID("11111111-1111-1111-1111-11111111a0a1")
    td = datetime.date(2026, 8, 25)
    correct_run = uuid.UUID("aaaa1111-1111-1111-1111-1111111111aa")
    wrong_run = uuid.UUID("bbbb2222-2222-2222-2222-2222222222bb")

    async with TestAsyncSessionLocal() as s:
        await _seed_instrument(s, iid, "SLC1A0A1")
        s.add_all([
            StockFeatureSnapshotRun(
                id=correct_run, trade_date=td, status="succeeded",
                run_type="after_close", published_at=datetime.datetime.utcnow(),
            ),
            StockFeatureSnapshotRun(
                id=wrong_run, trade_date=td, status="succeeded",
                run_type="after_close", published_at=datetime.datetime.utcnow(),
            ),
            StockFeatureSnapshot(
                instrument_id=iid, source_run_id=correct_run, trade_date=td,
                structural_payload={}, temporal_payload={},
                summary_payload={"first_pyramid_flat": {"fp_trend_direction": "上行"}},
            ),
            StockFeatureSnapshot(
                instrument_id=iid, source_run_id=wrong_run, trade_date=td,
                structural_payload={}, temporal_payload={},
                summary_payload={"first_pyramid_flat": {"fp_trend_direction": "下行"}},
            ),
        ])
        await s.commit()

    async with TestAsyncSessionLocal() as s:
        out = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=correct_run
        )
        out_wrong = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=wrong_run
        )

    # Loader 锁定 source_core_run_id：只返回该 run 的快照，整张 first_pyramid_flat 透传。
    assert out[str(iid)][_BOARD_CURRENT_FLAT_KEY]["fp_trend_direction"] == "上行"
    assert out_wrong[str(iid)][_BOARD_CURRENT_FLAT_KEY]["fp_trend_direction"] == "下行"
    # 关键：错误 run 不污染正确 run（无 fallback 到另一 same-day run）
    assert out[str(iid)][_BOARD_CURRENT_FLAT_KEY]["fp_trend_direction"] != "下行"
    assert out_wrong[str(iid)][_BOARD_CURRENT_FLAT_KEY]["fp_trend_direction"] != "上行"


async def test_current_facts_wrong_run_fails_closed():
    """Case C: source_core_run_id 指向不存在/无快照的 run → 空（fail-closed）。"""
    await _assert_verify_db_identity()

    iid = uuid.UUID("22222222-2222-2222-2222-22222222b0b1")
    td = datetime.date(2026, 8, 25)
    present_run = uuid.UUID("cccc3333-3333-3333-3333-33333333cc33")
    missing_run = uuid.UUID("dddd4444-4444-4444-4444-44444444dd44")

    async with TestAsyncSessionLocal() as s:
        await _seed_instrument(s, iid, "SLC2B0B1")
        s.add_all([
            StockFeatureSnapshotRun(
                id=present_run, trade_date=td, status="succeeded",
                run_type="after_close", published_at=datetime.datetime.utcnow(),
            ),
            StockFeatureSnapshot(
                instrument_id=iid, source_run_id=present_run, trade_date=td,
                structural_payload={}, temporal_payload={},
                summary_payload={"first_pyramid_flat": {"fp_trend_direction": "上行"}},
            ),
        ])
        await s.commit()

    async with TestAsyncSessionLocal() as s:
        out = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=missing_run
        )
    assert out == {}, "wrong core run must fail closed (empty), never fallback"
