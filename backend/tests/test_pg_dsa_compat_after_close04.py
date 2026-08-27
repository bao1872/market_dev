"""Targeted-PG closure (PG-A/B/C) for CORRECTION-04-PG-GATE.

Self-contained synthetic tests: calendar / instruments / bars / released DSA
version / persisted Core artifacts are ALL created by these tests inside the
verification database. NO production bz_stock fixture is read.
Registry: scripts/verify/verify_attempt.py -> run_self_contained_pg_tests
(仅追加注册；不动态 discovery；不扩大为全量 -m postgres)。

责任拆分：
- PG-A  required_compatibility create_batch_run 在 synthetic released version
        + market readiness 下：source_core_run_id==X、requirement==required_compatibility、
        total_instruments == synthetic universe count（2）。
- PG-B  给定真实 compatibility run + 可解码 persisted Core artifact，
        执行完整生产持久化链（不 mock）：
        StockFeatureSnapshot → iter_core_artifacts → decode_dsa_projection_from_summary
        → project_dsa_batch → persist_precomputed_dsa_results → run completed
        → quality gate → publish_run(db, run_id) → commit
        → 新 session 读回 status==published 且 published_at != null。
- PG-C  projection/persistence 失败（无可投影 artifact）：CoreRun.status 仍
        succeeded；MarketReviewRun.source_core_run_id 仍 == X；
        不产生 published 成功事实。

DB identity（fail-closed，测试自身要求）：
- APP_ENV == "verification"（非空）
- current_database() 匹配 ^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock
- 完整 SHA ↔ DB 一一对应由 verify_attempt.py identity gate 负责。
"""

import os
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.models.market_review import MarketReviewRun
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.stock_feature_snapshot_run_item import StockFeatureSnapshotRunItem
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    _run_dsa_compatibility_projection,
    _validate_core_ready,
)
from app.services.core_artifact_codec import (
    CORE_ARTIFACT_SCHEMA_VERSION,
    encode_core_artifact_to_summary,
)
from app.services.calendar_service import is_trading_day_async
from app.constants.strategy_keys import DSA_SELECTOR

pytestmark = pytest.mark.postgres

T = date(2026, 8, 26)

_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")


async def _assert_verify_db(db_session):
    """测试自身 fail-closed 身份检查。"""
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env == "verification", f"APP_ENV 必须 verification, got {env!r}"
    db_name = (await db_session.execute(text("select current_database()"))).scalar_one()
    assert _VERIFY_DB_RE.match(db_name), f"非法验证数据库: {db_name!r}"
    assert db_name != "bz_stock"
    return db_name


# ---------------------------------------------------------------------------
# self-contained synthetic universe（2 只股票 + 交易日历 + 当日 K 线）
# ---------------------------------------------------------------------------


async def _seed_calendar(db_session):
    T_prev = T - timedelta(days=1)
    for d, trading in ((T, True), (T_prev, True)):
        db_session.add(
            TradingCalendar(trade_date=d, is_trading_day=trading, market="A")
        )
    await db_session.flush()
    return T_prev


async def _seed_universe(db_session):
    inst_ids = []
    for i in range(2):  # 最小 synthetic universe：2 只股票
        sym = f"6{uuid.uuid4().hex[:5]}"[:6].ljust(6, "0")
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=sym,
            name=f"cor04pg_{i}",
            market="SH",
            status="active",
            listing_date=T - timedelta(days=3650),  # 非 new listing
        )
        db_session.add(inst)
        inst_ids.append(inst.id)
    return inst_ids


async def _seed_bars_for_coverage(db_session, inst_ids, dates):
    for iid in inst_ids:
        for d in dates:
            db_session.add(
                BarDaily(instrument_id=iid, trade_date=d, close=Decimal("10.00"))
            )


async def _seed_released_dsa_version(db_session):
    d = StrategyDefinition(
        strategy_key=f"test_dsa_cor04_{uuid.uuid4().hex[:8]}",
        kind="selector",
        display_name="CORRECTION-04-PG-GATE DSA compat",
    )
    db_session.add(d)
    await db_session.flush()
    v = StrategyVersion(
        strategy_definition_id=d.id,
        version="1.0.0",
        status="released",
        manifest={"outputs": [], "parameters": []},
        build_hash=uuid.uuid4().hex,
        released_at=datetime.now().astimezone(),
    )
    db_session.add(v)
    await db_session.flush()
    return v


def _artifact_summary_payload(seq, *, core_run_id, algo_ver):
    """生产 codec 构造可被 decode_dsa_projection_from_summary 真实解码的 artifact。"""
    block = encode_core_artifact_to_summary(
        schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
        first_pyramid_core={"trend": {"availability": "ready"}, "nBars": 250},
        structural_payload={"dsa_segment": {"seg": 1}},
        dsa_projection_payload={"dsa_vwap": 10.5, "dsa_dir_bars": 12},
        dsa_visual_contract={"dsa_vwap": 10.5},
        state_event_candidates=[{"type": "structure_break", "t": str(T)}],
        availability={
            "trend": "ready", "structure": "ready", "momentum": "ready",
        },
        parameter_hash=f"ph-{seq}",
        source_core_run_id=str(core_run_id),
        algorithm_versions={"dsa": algo_ver},
        input_hash=f"in-{seq}",
        bars_hash=f"bars-{seq}",
        adj_factor_hash=f"adj-{seq}",
        diagnostics={"dsa": 1, "smc": 1},
    )
    return {"coreArtifact": block}


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
        metadata_json="{}",
    )


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


# ---------------------------------------------------------------------------
# PG-A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_a_compat_create_batch_run_lineage_and_universe(db_session):
    await _assert_verify_db(db_session)

    _prev = await _seed_calendar(db_session)
    inst_ids = await _seed_universe(db_session)
    await _seed_bars_for_coverage(db_session, inst_ids, [T])
    version = await _seed_released_dsa_version(db_session)
    core_run = StockFeatureSnapshotRun(
        trade_date=T, run_type="after_close", status="succeeded",
        started_at=datetime.now().astimezone(),
        finished_at=datetime.now().astimezone(),
    )
    db_session.add(core_run)
    await db_session.commit()

    # readiness 自证：synthetic universe 必须处于就绪态
    assert await is_trading_day_async(db_session, T) is True

    from app.services.strategy_batch_service import StrategyBatchService

    svc = StrategyBatchService()
    run = await svc.create_batch_run(
        db=db_session,
        strategy_key=DSA_SELECTOR,
        trade_date=T,
        run_type="scheduled",
        instrument_ids=inst_ids,   # 真实 UUID 列表（[] 会造成 total=0）
        claim_for_worker="orchestrator:pg-a",
        source_core_run_id=core_run.id,
        requirement="required_compatibility",
    )
    await db_session.commit()

    assert run.source_core_run_id == core_run.id or (
        (run.input_overrides or {}).get("source_core_run_id") == str(core_run.id)
    ), "compatibility lineage 必须绑定 Core X"
    assert (run.input_overrides or {}).get("requirement") == "required_compatibility"
    assert run.total_instruments == len(inst_ids), (
        "total_instruments 必须 == synthetic universe count"
    )
    assert run.status in ("queued", "running")


# ---------------------------------------------------------------------------
# PG-B
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_b_full_projection_publish_readback(db_session):
    from uuid import UUID

    await _assert_verify_db(db_session)

    _prev = await _seed_calendar(db_session)
    inst_ids = await _seed_universe(db_session)
    await _seed_bars_for_coverage(db_session, inst_ids, [T])
    version = await _seed_released_dsa_version(db_session)
    core_run = StockFeatureSnapshotRun(
        trade_date=T, run_type="after_close", status="succeeded",
        started_at=datetime.now().astimezone(),
        finished_at=datetime.now().astimezone(),
    )
    db_session.add(core_run)
    await db_session.flush()

    review_row = _make_review_run(core_run)
    db_session.add(review_row)
    job = _make_job_row()
    db_session.add(job)
    for iid in inst_ids:
        db_session.add(
            StockFeatureSnapshotRunItem(
                snapshot_run_id=core_run.id, instrument_id=iid,
                phase="core", status="succeeded",
            )
        )
        # 真实可 decode 的 versioned Core artifact（生产 codec 构造）
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=iid,
                trade_date=T,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                source_run_id=core_run.id,
                structural_payload=_artifact_summary_payload(
                    str(iid)[:8], core_run_id=core_run.id,
                    algo_ver=version.version,
                ),
                temporal_payload={},
                summary_payload={},
            )
        )
    await db_session.commit()

    async with AsyncSessionLocal() as vdb:
        validated = await _validate_core_ready(vdb, core_run.id, T)
        assert validated.status == "succeeded"

    # 完整生产 helper 链：无任何持久化环节 mock
    result = await _run_dsa_compatibility_projection(
        job_run_id=job.id,
        worker_id="pg-b",
        lease_epoch=None,
        trade_date=T,
        snapshot_run_id=core_run.id,
        dsa_run_id=None,
        instrument_ids=inst_ids,
    )
    assert result["status"] == "succeeded", result
    assert result.get("published_at")

    async with AsyncSessionLocal() as pdb:
        row = await pdb.get(StrategyRun, UUID(result["dsa_run_id"]))
        assert row.status == "published"
        assert row.published_at is not None
        assert (row.input_overrides or {}).get("requirement") == "required_compatibility"


# ---------------------------------------------------------------------------
# PG-C
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_c_projection_failure_preserves_lineage(db_session):
    from uuid import UUID

    await _assert_verify_db(db_session)

    await _seed_calendar(db_session)
    await _seed_universe(db_session)
    version = await _seed_released_dsa_version(db_session)
    core_run = StockFeatureSnapshotRun(
        trade_date=T, run_type="after_close", status="succeeded",
        started_at=datetime.now().astimezone(),
        finished_at=datetime.now().astimezone(),
    )
    db_session.add(core_run)
    review_row = _make_review_run(core_run)
    db_session.add(review_row)
    job = _make_job_row()
    db_session.add(job)
    # 无 snapshot 行 → 投影零产物 → 未达 completed → RuntimeError
    await db_session.commit()

    with pytest.raises(RuntimeError):
        await _run_dsa_compatibility_projection(
            job_run_id=job.id,
            worker_id="pg-c",
            lease_epoch=None,
            trade_date=T,
            snapshot_run_id=core_run.id,
            dsa_run_id=None,
            instrument_ids=[],
        )

    async with AsyncSessionLocal() as cdb:
        c2 = await cdb.get(StockFeatureSnapshotRun, core_run.id)
        assert c2.status == "succeeded", "Core 不被兼容性失败撤销"
        rr = (
            await cdb.execute(
                select(MarketReviewRun).where(
                    MarketReviewRun.source_core_run_id == core_run.id,
                )
            )
        ).scalars().all()
        assert rr and all(
            r.source_core_run_id == core_run.id and r.status == "published"
            for r in rr
        ), "Review lineage 保持 source_core_run_id == X"

        compat_runs = (
            await cdb.execute(
                select(StrategyRun).where(
                    StrategyRun.trade_date == T,
                    StrategyRun.requirement == "required_compatibility",
                )
            )
        ).scalars().all()
        assert compat_runs and all(
            r.status != "published" for r in compat_runs
        ), "兼容性不得产生 published 成功事实"
