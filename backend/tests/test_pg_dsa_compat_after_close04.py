"""Targeted-PG closure (PG-A/B/C) for CORRECTION-04-PG-GATE.

Self-contained synthetic tests: calendar / instruments / bars / released DSA
version / persisted Core artifacts are ALL created by these tests inside the
verification database (bz_stock_verify_<SHA>) via REAL AsyncSessionLocal
connections (proven pattern: committed rows must be visible to production
helpers that open their own sessions). NO production bz_stock data is read or
written; the whole verify database is dropped by the gate cleanup.
Registry: scripts/verify/verify_attempt.py -> run_self_contained_pg_tests.

责任拆分：
- PG-A  required_compatibility create_batch_run 在 synthetic released version
        + market readiness 下：lineage 绑定 Core X、requirement、
        total_instruments == synthetic universe count（2）。
- PG-B  完整生产持久化链（不 mock）：
        StockFeatureSnapshot(真实 codec artifact)
        → iter_core_artifacts → decode_dsa_projection_from_summary
        → project_dsa_batch → persist_precomputed_dsa_results → completed
        → quality gate → publish_run(db, run_id) → commit
        → 新 session 读回 status==published 且 published_at != null。
- PG-C  projection/persistence 失败（无可投影 artifact）：CoreRun.status 仍
        succeeded；MarketReviewRun.source_core_run_id 仍 == X；
        不产生 published 成功事实。

DB identity（fail-closed，测试自身要求）：
- APP_ENV == "verification"（非空）
- current_database() 匹配 ^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock
"""

import os
import re
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.constants.strategy_keys import DSA_SELECTOR
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

pytestmark = pytest.mark.postgres

T = date(2026, 8, 26)

_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")


async def _assert_verify_db(db):
    """测试自身 fail-closed 身份检查（直连 session）。"""
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env == "verification", f"APP_ENV 必须 verification, got {env!r}"
    db_name = (await db.execute(text("select current_database()"))).scalar_one()
    assert _VERIFY_DB_RE.match(db_name), f"非法验证数据库: {db_name!r}"
    assert db_name != "bz_stock"
    return db_name


# ---------------------------------------------------------------------------
# self-contained synthetic universe（2 只股票 + 交易日历 + 当日 K 线）
# 全部经 AsyncSessionLocal 直连提交 —— 生产 helper 开独立 session 必须可见。
# ---------------------------------------------------------------------------


async def _seed_base_world():
    """一次性预置 calendar + instruments + bars + released DSA version。

    返回 (inst_ids, strategy_version_row)。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        t_prev = T - timedelta(days=1)
        # calendar 幂等（唯一约束 trade_date+market；多用例共享验证库）
        for d in (T, t_prev):
            exists = (
                await s.execute(
                    select(TradingCalendar).where(
                        TradingCalendar.trade_date == d,
                        TradingCalendar.market == "A",
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                s.add(TradingCalendar(trade_date=d, is_trading_day=True, market="A"))
        inst_ids = []
        for i in range(2):  # 最小 synthetic universe：2 只股票
            # SH 规则 ^6[0-9]{5}$ —— 必须是纯数字否则 stock_symbol_sql_filter 排除
            sym = "6" + f"{uuid.uuid4().int % 100000:05d}"
            inst = Instrument(
                id=uuid.uuid4(),
                symbol=sym,
                name=f"cor04pg_{i}",
                market="SH",
                status="active",
                listing_date=T - timedelta(days=3650),  # 非 new listing
            )
            s.add(inst)
            inst_ids.append(inst.id)
        # [PG-GATE] 先提交 calendar+instruments：BarDaily 与 Instrument 间无
        # ORM relationship，同 unit-of-work 的 INSERT 组顺序不保证依赖序，
        # 必须让 instruments 先行落库，bars 才能通过 FK 检查。
        await s.commit()

    async with AsyncSessionLocal() as s:
        for iid in inst_ids:
            s.add(
                BarDaily(
                    instrument_id=iid, trade_date=T,
                    close=Decimal("10.00"), adj_factor=Decimal("1.0"),
                )
            )
        # canonical DSA definition（create_batch_run 以 strategy_key 解析策略；
        # 验证库为空库，必须自备 dsa_selector 定义 + released version，且幂等）
        d_def = (
            await s.execute(
                select(StrategyDefinition).where(
                    StrategyDefinition.strategy_key == DSA_SELECTOR,
                )
            )
        ).scalar_one_or_none()
        if d_def is None:
            d_def = StrategyDefinition(
                strategy_key=DSA_SELECTOR,
                kind="selector",
                display_name="CORRECTION-04-PG-GATE DSA compat",
            )
            s.add(d_def)
            await s.flush()
        version = (
            await s.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_definition_id == d_def.id,
                    StrategyVersion.version == "1.0.0",
                    StrategyVersion.status == "released",
                )
            )
        ).scalar_one_or_none()
        if version is None:
            version = StrategyVersion(
                strategy_definition_id=d_def.id,
                version="1.0.0",
                status="released",
                manifest={"outputs": [], "parameters": []},
                build_hash=uuid.uuid4().hex,
                released_at=datetime.now().astimezone(),
            )
            s.add(version)
        await s.commit()
    return inst_ids, version


async def _make_core_run_with_snapshots(inst_ids, version):
    """succeeded CoreRun + core run items + 真实可解码 artifact snapshots。"""
    async with AsyncSessionLocal() as s:
        run = StockFeatureSnapshotRun(
            trade_date=T, run_type="after_close", status="succeeded",
            started_at=datetime.now().astimezone(),
            finished_at=datetime.now().astimezone(),
        )
        s.add(run)
        await s.flush()
        for idx, iid in enumerate(inst_ids):
            s.add(
                StockFeatureSnapshotRunItem(
                    snapshot_run_id=run.id, instrument_id=iid,
                    phase="core", status="succeeded",
                )
            )
            # 生产 codec 构造可被 decode_dsa_projection_from_summary 真实解码的 payload
            block = encode_core_artifact_to_summary(
                schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
                first_pyramid_core={"trend": {"availability": "ready"}, "nBars": 250},
                structural_payload={"dsa_segment": {"seg": idx + 1}},
                dsa_projection_payload={"dsa_vwap": 10.5, "dsa_dir_bars": 12},
                dsa_visual_contract={"dsa_vwap": 10.5},
                state_event_candidates=[
                    {"type": "structure_break", "t": str(T)},
                ],
                availability={
                    "trend": "ready", "structure": "ready", "momentum": "ready",
                },
                parameter_hash=f"ph-{idx}",
                source_core_run_id=str(run.id),
                algorithm_versions={"dsa": version.version},
                input_hash=f"in-{idx}",
                bars_hash=f"bars-{idx}",
                adj_factor_hash=f"adj-{idx}",
                diagnostics={"dsa": 1, "smc": 1},
            )
            s.add(
                StockFeatureSnapshot(
                    instrument_id=iid,
                    trade_date=T,
                    primary_timeframe="1d",
                    secondary_timeframe="15m",
                    adj="qfq",
                    schema_version=1,
                    source_run_id=run.id,
                    structural_payload={"coreArtifact": block},
                    temporal_payload={},
                    summary_payload={},
                )
            )
        await s.flush()  # 确保 run.id 已生成
        review = MarketReviewRun(
            trade_date=T,
            source_core_run_id=run.id,
            source_board_run_id=None,
            source_chip_run_id=None,
            degraded_reasons=[],
            algorithm_version="review-1.0.0",
            filter_version="filters-1.0.0",
            expected_scope_count=0,
            succeeded_scope_count=0,
            failed_scope_count=0,
            signal_count=0,
            coverage_ratio=Decimal("1.0"),
            status="published",
        )
        s.add(review)
        job = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date=T.isoformat(),
            run_key=f"after_close_orchestrator:pg_cor04:{uuid.uuid4().hex[:8]}",
            status="running",
            scheduled_at=datetime.now().astimezone(),
            started_at=datetime.now().astimezone(),
            heartbeat_at=datetime.now().astimezone(),
            lease_expires_at=datetime.now().astimezone(),
            metadata_json="{}",
        )
        s.add(job)
        await s.commit()
        return run.id, job.id


def _make_review_only_job():
    now = datetime.now().astimezone()

    async def _create():
        async with AsyncSessionLocal() as s:
            job = SchedulerJobRun(
                job_name="after_close_orchestrator",
                business_date=T.isoformat(),
                run_key=f"after_close_orchestrator:pg_cor04_c:{uuid.uuid4().hex[:8]}",
                status="running",
                scheduled_at=now,
                started_at=now,
                heartbeat_at=now,
                lease_expires_at=now,
                metadata_json="{}",
            )
            s.add(job)
            await s.commit()
            return job.id

    return _create


def _make_review_run(core_run_id):
    return MarketReviewRun(
        trade_date=T,
        source_core_run_id=core_run_id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version="review-1.0.0",
        filter_version="filters-1.0.0",
        expected_scope_count=0,
        succeeded_scope_count=0,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=Decimal("1.0"),
        status="published",
    )


# ---------------------------------------------------------------------------
# PG-A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_a_compat_create_batch_run_lineage_and_universe():
    inst_ids, _ver = await _seed_base_world()
    async with AsyncSessionLocal() as s:
        run = StockFeatureSnapshotRun(
            trade_date=T, run_type="after_close", status="succeeded",
            started_at=datetime.now().astimezone(),
            finished_at=datetime.now().astimezone(),
        )
        s.add(run)
        await s.commit()
        core_x = run.id

    from app.services.strategy_batch_service import StrategyBatchService

    svc = StrategyBatchService()
    compat = await svc.create_batch_run(
        db=s,
        strategy_key=DSA_SELECTOR,
        trade_date=T,
        run_type="scheduled",
        instrument_ids=list(map(str, inst_ids)),
        claim_for_worker="orchestrator:pg-a",
        source_core_run_id=core_x,
        requirement="required_compatibility",
    )
    await s.commit()

    assert (compat.input_overrides or {}).get("source_core_run_id") == str(core_x), (
        "compatibility lineage 必须绑定 Core X"
    )
    assert (compat.input_overrides or {}).get("requirement") == "required_compatibility"
    assert compat.total_instruments == len(inst_ids), (
        "total_instruments 必须 == synthetic universe count"
    )


# ---------------------------------------------------------------------------
# PG-B
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_b_full_projection_publish_readback():
    from uuid import UUID

    inst_ids, version = await _seed_base_world()
    core_x, job_id = await _make_core_run_with_snapshots(inst_ids, version)

    # 点1: canonical owner 对真实行判定成功
    async with AsyncSessionLocal() as vdb:
        validated = await _validate_core_ready(vdb, core_x, T)
        assert validated.status == "succeeded"

    result = await _run_dsa_compatibility_projection(
        job_run_id=job_id,
        worker_id="pg-b",
        lease_epoch=None,
        trade_date=T,
        snapshot_run_id=core_x,
        dsa_run_id=None,
        instrument_ids=[str(i) for i in inst_ids],
    )
    assert result["status"] == "succeeded", result
    assert result.get("published_at")

    # 新 session 读回持久化事实
    async with AsyncSessionLocal() as pdb:
        row = await pdb.get(StrategyRun, UUID(result["dsa_run_id"]))
        assert row.status == "published"
        assert row.published_at is not None
        assert (row.input_overrides or {}).get("requirement") == "required_compatibility"


# ---------------------------------------------------------------------------
# PG-C
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_c_projection_failure_preserves_lineage():

    inst_ids, _ver = await _seed_base_world()
    # succeeded CoreRun，但**无任何 snapshot 行** → 投影零产物 → 未达 completed → raise
    async with AsyncSessionLocal() as s:
        run = StockFeatureSnapshotRun(
            trade_date=T, run_type="after_close", status="succeeded",
            started_at=datetime.now().astimezone(),
            finished_at=datetime.now().astimezone(),
        )
        s.add(run)
        await s.flush()  # 生成 run.id 后再挂 Review lineage
        s.add(_make_review_run(run.id))
        job = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date=T.isoformat(),
            run_key=f"after_close_orchestrator:pg_cor04_c:{uuid.uuid4().hex[:8]}",
            status="running",
            scheduled_at=datetime.now().astimezone(),
            started_at=datetime.now().astimezone(),
            heartbeat_at=datetime.now().astimezone(),
            lease_expires_at=datetime.now().astimezone(),
            metadata_json="{}",
        )
        s.add(job)
        await s.commit()
        core_x, job_id = run.id, job.id

    with pytest.raises(RuntimeError):
        await _run_dsa_compatibility_projection(
            job_run_id=job_id,
            worker_id="pg-c",
            lease_epoch=None,
            trade_date=T,
            snapshot_run_id=core_x,
            dsa_run_id=None,
            instrument_ids=[],
        )

    # Core 不被撤销；Review lineage 保持 source_core_run_id == X
    async with AsyncSessionLocal() as cdb:
        c2 = await cdb.get(StockFeatureSnapshotRun, core_x)
        assert c2.status == "succeeded", "Core 不被兼容性失败撤销"
        rr = (
            await cdb.execute(
                select(MarketReviewRun).where(
                    MarketReviewRun.source_core_run_id == core_x,
                )
            )
        ).scalars().all()
        assert rr and all(
            r.source_core_run_id == core_x and r.status == "published" for r in rr
        ), "Review lineage 保持"

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
