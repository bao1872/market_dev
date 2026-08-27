"""Slice 1 (REVIEW-CURRENT-OWNER-01) POSTGRES tests — current facts locked to Core run.

[CHANGE-20260826-001 Slice 1 / CORRECTION-05] Review(T) current First Pyramid facts come
ONLY from the exact-T StockFeatureSnapshot(T) owned by source_core_run_id.  Readiness =
Core Ready X (``StockFeatureSnapshotRun.status == succeeded`` + ``trade_date == T``).
publication (``published_at`` / stock_core pointer) is NOT part of Review current-fact
readiness.

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
    错误 run 被忽略（无 fallback）。

    注意：stock_feature_snapshots 唯一键为
    (instrument_id, trade_date, primary_timeframe, secondary_timeframe, adj,
     schema_version)，不含 source_run_id —— 即每个 instrument+date 只有一行快照，
    source_run_id 标识其来源 run。因此用两个不同 instrument 分别代表
    correct_run / wrong_run 的快照，验证 loader 按 source_core_run_id 精确选取。
    """
    await _assert_verify_db_identity()

    iid_a = uuid.UUID("11111111-1111-1111-1111-11111111a0a1")
    iid_b = uuid.UUID("11111111-1111-1111-1111-11111111b0b1")
    td = datetime.date(2026, 8, 25)
    correct_run = uuid.UUID("aaaa1111-1111-1111-1111-1111111111aa")
    wrong_run = uuid.UUID("bbbb2222-2222-2222-2222-2222222222bb")

    async with TestAsyncSessionLocal() as s:
        await _seed_instrument(s, iid_a, "SLC1A0A1")
        await _seed_instrument(s, iid_b, "SLC1B0B1")
        await s.flush()
        # §7B: positive case 必须使用 published_at=None 的 Core Ready（status=succeeded），
        # 证明 publication 不参与 Review current-fact readiness。
        s.add(StockFeatureSnapshotRun(
            id=correct_run, trade_date=td, status="succeeded",
            run_type="after_close", published_at=None,
        ))
        s.add(StockFeatureSnapshotRun(
            id=wrong_run, trade_date=td, status="succeeded",
            run_type="after_close", published_at=None,
        ))
        await s.flush()
        # iid_a 的快照来自 correct_run（上行）；iid_b 的快照来自 wrong_run（下行）。
        s.add(StockFeatureSnapshot(
            instrument_id=iid_a, source_run_id=correct_run, trade_date=td,
            structural_payload={}, temporal_payload={},
            summary_payload={"first_pyramid_flat": {"fp_trend_direction": "上行"}},
        ))
        s.add(StockFeatureSnapshot(
            instrument_id=iid_b, source_run_id=wrong_run, trade_date=td,
            structural_payload={}, temporal_payload={},
            summary_payload={"first_pyramid_flat": {"fp_trend_direction": "下行"}},
        ))
        await s.commit()

    async with TestAsyncSessionLocal() as s:
        out = await _load_current_only_snapshot_facts(
            s, [iid_a, iid_b], td, source_core_run_id=correct_run
        )
        out_wrong = await _load_current_only_snapshot_facts(
            s, [iid_a, iid_b], td, source_core_run_id=wrong_run
        )

    # §7A KPI-9: loader owner 返回 str(UUID) key contract；禁止用裸 UUID 判等。
    # source_core_run_id=correct_run → 只返回 iid_a（上行），不含 iid_b（下行）。
    assert str(iid_a) in out
    assert str(iid_b) not in out
    assert out[str(iid_a)][_BOARD_CURRENT_FLAT_KEY]["fp_trend_direction"] == "上行"
    # source_core_run_id=wrong_run → 只返回 iid_b（下行），不含 iid_a（上行）。
    assert str(iid_b) in out_wrong
    assert str(iid_a) not in out_wrong
    assert out_wrong[str(iid_b)][_BOARD_CURRENT_FLAT_KEY]["fp_trend_direction"] == "下行"
    # §7B: published_at=None 不影响 Review 对明确 source_core_run_id X 的读取
    # （两个 run 均为 succeeded + published_at=None，仍被正常消费）。
    # 关键：错误 run 不污染正确 run（无 fallback 到另一 run 的快照）
    assert "下行" not in str(out)
    assert "上行" not in str(out_wrong)


async def test_current_facts_wrong_run_fails_closed():
    """Case C: source_core_run_id 指向不存在/无快照的 run → 空（fail-closed）。"""
    await _assert_verify_db_identity()

    iid = uuid.UUID("22222222-2222-2222-2222-22222222b0b1")
    td = datetime.date(2026, 8, 25)
    present_run = uuid.UUID("cccc3333-3333-3333-3333-33333333cc33")
    missing_run = uuid.UUID("dddd4444-4444-4444-4444-44444444dd44")

    async with TestAsyncSessionLocal() as s:
        await _seed_instrument(s, iid, "SLC2B0B1")
        await s.flush()
        s.add(StockFeatureSnapshotRun(
            id=present_run, trade_date=td, status="succeeded",
            run_type="after_close", published_at=None,
        ))
        await s.flush()
        s.add(StockFeatureSnapshot(
            instrument_id=iid, source_run_id=present_run, trade_date=td,
            structural_payload={}, temporal_payload={},
            summary_payload={"first_pyramid_flat": {"fp_trend_direction": "上行"}},
        ))
        await s.commit()

    async with TestAsyncSessionLocal() as s:
        out = await _load_current_only_snapshot_facts(
            s, [iid], td, source_core_run_id=missing_run
        )
    assert out == {}, "wrong core run must fail closed (empty), never fallback"


async def test_current_facts_running_core_fails_closed():
    """§8 KPI-3: Core X.status=running → 返回空，不得消费。

    删除 published_at gate 后，仍必须要求 status==succeeded；
    running Core（即便 published_at=None）不得被 Review 当前事实消费。
    """
    await _assert_verify_db_identity()

    iid = uuid.UUID("33333333-3333-3333-3333-33333333c0c1")
    td = datetime.date(2026, 8, 25)
    run = uuid.UUID("eeee5555-5555-5555-5555-5555555555ee")

    async with TestAsyncSessionLocal() as s:
        await _seed_instrument(s, iid, "SLC3C0C1")
        await s.flush()
        s.add(StockFeatureSnapshotRun(
            id=run, trade_date=td, status="running",
            run_type="after_close", published_at=None,
        ))
        await s.flush()
        s.add(StockFeatureSnapshot(
            instrument_id=iid, source_run_id=run, trade_date=td,
            structural_payload={}, temporal_payload={},
            summary_payload={"first_pyramid_flat": {"fp_trend_direction": "上行"}},
        ))
        await s.commit()

    async with TestAsyncSessionLocal() as s:
        out = await _load_current_only_snapshot_facts(s, [iid], td, source_core_run_id=run)
    assert out == {}, "running Core must NOT be consumable (fails closed)"


async def test_current_facts_failed_core_fails_closed():
    """§8 KPI-3: Core X.status=failed → 返回空，不得消费。"""
    await _assert_verify_db_identity()

    iid = uuid.UUID("44444444-4444-4444-4444-44444444d0d1")
    td = datetime.date(2026, 8, 25)
    run = uuid.UUID("ffff6666-6666-6666-6666-6666666666ff")

    async with TestAsyncSessionLocal() as s:
        await _seed_instrument(s, iid, "SLC4D0D1")
        await s.flush()
        s.add(StockFeatureSnapshotRun(
            id=run, trade_date=td, status="failed",
            run_type="after_close", published_at=None,
        ))
        await s.flush()
        s.add(StockFeatureSnapshot(
            instrument_id=iid, source_run_id=run, trade_date=td,
            structural_payload={}, temporal_payload={},
            summary_payload={"first_pyramid_flat": {"fp_trend_direction": "上行"}},
        ))
        await s.commit()

    async with TestAsyncSessionLocal() as s:
        out = await _load_current_only_snapshot_facts(s, [iid], td, source_core_run_id=run)
    assert out == {}, "failed Core must NOT be consumable (fails closed)"
