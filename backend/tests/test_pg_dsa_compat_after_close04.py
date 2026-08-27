"""Targeted-PG closure (1-7) for CORRECTION-04 DSA compatibility contract.

Synthetic data only. NO production bz_stock fixture is read.
Registry: scripts/verify/verify_attempt.py -> run_self_contained_pg_tests
(仅追加注册；不动态 discovery；不扩大为全量 -m postgres)。

审计十六最小覆盖：
1. CoreRun succeeded
2. 创建 required_compatibility DSA run（helper dsa_run_id=None 路径）
3. 执行 persisted Core artifact projection（project_dsa_batch）
4. publish_run(db, run_id)（真实签名，内部只 flush）
5. commit（helper 显式提交）
6. 新 session 读回：StrategyRun.status == published 且 published_at != null
7. DSA failure 场景：helper raise（投影无产物/未达 completed）→
   CoreRun.status 仍 succeeded；MarketReviewRun(source_core_run_id=X)
   lineage 不被撤销；兼容性 run 如实标 failed。

DB identity fail-closed：APP_ENV=verification 且 current_database() ==
bz_stock_verify_<sha> 且 != bz_stock。
"""

import json
import os
import uuid
from datetime import date, datetime

import pytest
from sqlalchemy import select, text

from app.db import AsyncSessionLocal
from app.models.market_review import MarketReviewRun
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    _run_dsa_compatibility_projection,
    _validate_core_ready,
)

pytestmark = pytest.mark.postgres

T = date(2026, 8, 26)


async def _assert_verify_db(db_session):
    """fail-closed：必须处于 bz_stock_verify_<sha>，绝不允许连到 bz_stock。"""
    row = (
        await db_session.execute(
            text("select current_database(), current_setting('app.env', true)")
        )
    ).one()
    db_name = row[0]
    assert db_name.startswith("bz_stock_verify_"), (
        f"非法验证数据库: {db_name!r}（必须是 bz_stock_verify_<sha>）"
    )
    assert db_name != "bz_stock"
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env in ("verification", ""), f"APP_ENV 必须 verification, got {env!r}"
    return db_name


async def _make_instruments(db_session, n=2):
    from app.models.instrument import Instrument

    out = []
    for i in range(n):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"T{uuid.uuid4().hex[:16]}",
            name=f"cor04_{i}",
            market="SZ",
            status="active",
            listing_date=date(2010, 1, 4),
        )
        db_session.add(inst)
        out.append(inst.id)
    await db_session.flush()
    return out


async def _make_core_run_with_snapshots(db_session, n=2):
    """点1 前置：StockFeatureSnapshotRun(succeeded) + core run items + snapshots。"""
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run_item import (
        StockFeatureSnapshotRunItem,
    )

    inst_ids = await _make_instruments(db_session, n=n)
    run = StockFeatureSnapshotRun(
        trade_date=T,
        run_type="after_close",
        status="succeeded",
        started_at=datetime.now().astimezone(),
        finished_at=datetime.now().astimezone(),
    )
    db_session.add(run)
    await db_session.flush()
    for iid in inst_ids:
        db_session.add(
            StockFeatureSnapshotRunItem(
                snapshot_run_id=run.id, instrument_id=iid,
                phase="core", status="succeeded",
            )
        )
        # 三键 payload 与生产写入路径同构（proven 模式：runtime_blocker_closure）
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=iid,
                trade_date=T,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                source_run_id=run.id,
                structural_payload={"ok": True},
                temporal_payload={"ok": True},
                summary_payload={"ok": True},
            )
        )
    await db_session.flush()
    return run


def _make_review_run(core_run):
    return MarketReviewRun(
        trade_date=T,
        source_core_run_id=core_run.id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version="review-1.0.0",
        filter_version="filters-1.0.0",
        status="published",
    )


def _make_job_row():
    now = datetime.now().astimezone()
    return SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=T.isoformat(),
        run_key=f"after_close_orchestrator:pg_cor04:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        metadata_json=json.dumps({}),
    )


@pytest.mark.asyncio
async def test_pg_cor04_points_1_to_6_publish_roundtrip(db_session) -> None:
    """点1-6: Core succeeded → 兼容 run 创建/投影/publish_run(db,id)/commit →
    新会话读回 published；Core/Review lineage 保持完好。"""
    await _assert_verify_db(db_session)

    core_run = await _make_core_run_with_snapshots(db_session)
    review_row = _make_review_run(core_run)
    db_session.add(review_row)
    job = _make_job_row()
    db_session.add(job)
    await db_session.commit()  # 让独立 session 可见基线数据

    # 点1: canonical owner 对真实行判定成功
    async with AsyncSessionLocal() as vdb:
        validated = await _validate_core_ready(vdb, core_run.id, T)
        assert validated.status == "succeeded"

    # 点2-5: helper 全链（required_compatibility run 创建 → 投影 → publish_run → commit）
    result = await _run_dsa_compatibility_projection(
        job_run_id=job.id,
        worker_id="pg-cor04",
        lease_epoch=None,
        trade_date=T,
        snapshot_run_id=core_run.id,
        dsa_run_id=None,
        instrument_ids=[],
    )
    assert result["status"] == "succeeded", result
    assert result.get("published_at")  # 点5: helper 显式 commit 后取得发布时间

    # 点6: 新开 session 读回持久化事实
    async with AsyncSessionLocal() as pdb:
        row = await pdb.get(StrategyRun, uuid.UUID(result["dsa_run_id"]))
        assert row is not None
        assert row.status == "published"
        assert row.published_at is not None
        assert row.requirement == "required_compatibility"

    # Core / Review lineage 未被兼容性工作破坏
    async with AsyncSessionLocal() as cdb:
        c2 = await cdb.get(StockFeatureSnapshotRun, core_run.id)
        assert c2.status == "succeeded"
        r2 = await cdb.get(MarketReviewRun, review_row.id)
        assert r2.status == "published"


@pytest.mark.asyncio
async def test_pg_cor04_point7_dsa_failure_preserves_lineage(db_session) -> None:
    """点7: 投影无产物（无 snapshot 行）→ helper RuntimeError；
    CoreRun.status 保持 succeeded；Review lineage 不被撤销；兼容 run 标 failed。"""
    await _assert_verify_db(db_session)

    core_run = StockFeatureSnapshotRun(
        trade_date=T,
        run_type="after_close",
        status="succeeded",
        started_at=datetime.now().astimezone(),
        finished_at=datetime.now().astimezone(),
    )
    db_session.add(core_run)
    review_row = _make_review_run(core_run)
    db_session.add(review_row)
    job = _make_job_row()
    db_session.add(job)
    await db_session.commit()

    with pytest.raises(RuntimeError):
        await _run_dsa_compatibility_projection(
            job_run_id=job.id,
            worker_id="pg-cor04",
            lease_epoch=None,
            trade_date=T,
            snapshot_run_id=core_run.id,
            dsa_run_id=None,
            instrument_ids=[],
        )

    # Core 不被撤销；Review lineage 完好
    async with AsyncSessionLocal() as cdb:
        c2 = await cdb.get(StockFeatureSnapshotRun, core_run.id)
        assert c2.status == "succeeded"
        rr = (
            await cdb.execute(
                select(MarketReviewRun).where(
                    MarketReviewRun.source_core_run_id == core_run.id,
                )
            )
        ).scalars().all()
        assert rr and all(r.status == "published" for r in rr)

    # 兼容性 run 已如实标 failed（不得伪造 completed/published）
    async with AsyncSessionLocal() as sdb:
        rows = (
            await sdb.execute(
                select(StrategyRun).where(
                    StrategyRun.trade_date == T,
                    StrategyRun.requirement == "required_compatibility",
                )
            )
        ).scalars().all()
        assert rows and all(r.status == "failed" for r in rows)
