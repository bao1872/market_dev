"""Targeted-PG closure (PG-A/B/C) for CORRECTION-04-PG-FIXTURE-CORRECTION-01.

Self-contained synthetic tests: calendar / instruments / bars / released DSA
version / persisted Core artifacts are ALL created by these tests inside the
verification database (bz_stock_verify_<SHA>) via REAL AsyncSessionLocal
connections (proven pattern: committed rows must be visible to production
helpers that open their own sessions). NO production bz_stock data is read or
written; the whole verify database is dropped by the gate cleanup.
Registry: scripts/verify/verify_attempt.py -> run_self_contained_pg_tests.

职责拆分（本轮只证明数据库层，不证明 Review domain）：
- PG-A  required_compatibility create_batch_run 在 synthetic released version
        + market readiness + >=60 根历史日线 下：lineage 绑定 Core X、
        requirement、total_instruments == 2，且 run_items 全 pending（非
        insufficient_history skipped）。
- PG-B  完整生产持久化链（不 mock）：
        StockFeatureSnapshot(真实 codec artifact: summary_payload 同时含
        coreArtifact 与 dsaProjection)
        → iter_core_artifacts → decode_dsa_projection_from_summary
        → project_dsa_batch → persist_precomputed_dsa_results
        → quality gate → publish_run(db, run_id) → commit
        → 新 session 读回 status==published 且 published_at != null。
- PG-C  projection/persistence 失败（可投影 artifact 缺失，但有真实 universe）：
        CoreRun.status 仍 succeeded；兼容 StrategyRun 不产生 published 成功事实。

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
from sqlalchemy import func, select, text

from app.constants.strategy_keys import DSA_SELECTOR
from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.stock_feature_snapshot_run_item import StockFeatureSnapshotRunItem
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyResult, StrategyRun, StrategyRunItem
from app.services.after_close_orchestrator import (
    _run_dsa_compatibility_projection,
    _validate_core_ready,
)
from app.services.core_artifact_codec import (
    CORE_ARTIFACT_SCHEMA_VERSION,
    encode_core_artifact_to_summary,
    encode_dsa_projection_to_summary,
)

pytestmark = pytest.mark.postgres

T = date(2026, 8, 26)

# 确定性合法 SH A 股代码（严格满足 ^6[0-9]{5}$，含科创板 688xxx）。
# 不再随机生成，多用例共享验证库时按 symbol 幂等复用，避免 unique 冲突。
SYNTH_SYMBOLS = ("688991", "688992")

# DSA 正常路径要求每只标的 >= 60 根历史日线（StrategyBatchService._DSA_MIN_HISTORY_BARS）。
HISTORY_BARS = 60

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
# self-contained synthetic universe（2 只股票 + 交易日历 + >=60 根历史 K 线）
# 全部经 AsyncSessionLocal 直连提交 —— 生产 helper 开独立 session 必须可见。
# 全部按 (symbol / unique key) 幂等：同一验证库内多个 test case 重复调用不冲突。
# ---------------------------------------------------------------------------


async def _seed_base_world():
    """一次性预置 calendar + instruments + bars + released DSA version。

    返回 (inst_ids, strategy_version_row)。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        # calendar 幂等：仅 T 为交易日（多用例共享验证库；不插历史交易日可避免
        # check_data_readiness 的 import_completeness 与前一日 K 线数量比较）。
        cal = (
            await s.execute(
                select(TradingCalendar).where(
                    TradingCalendar.trade_date == T,
                    TradingCalendar.market == "A",
                )
            )
        ).scalar_one_or_none()
        if cal is None:
            s.add(TradingCalendar(trade_date=T, is_trading_day=True, market="A"))

        inst_ids: list[uuid.UUID] = []
        for sym in SYNTH_SYMBOLS:
            # 按 symbol 幂等复用，避免随机 unique 冲突。
            inst = (
                await s.execute(
                    select(Instrument).where(
                        Instrument.symbol == sym, Instrument.market == "SH",
                    )
                )
            ).scalar_one_or_none()
            if inst is None:
                inst = Instrument(
                    id=uuid.uuid4(),
                    symbol=sym,
                    name=f"cor04pg_{sym}",
                    market="SH",
                    status="active",
                    listing_date=T - timedelta(days=3650),  # 非 new listing
                )
                s.add(inst)
                await s.flush()
            else:
                # 复用既有时确保为 active，使 check_data_readiness 纳入活跃分母。
                inst.status = "active"
            inst_ids.append(inst.id)

        # [PG-GATE] 先提交 calendar+instruments：BarDaily 与 Instrument 间无
        # ORM relationship，同 unit-of-work 的 INSERT 组顺序不保证依赖序，
        # 必须让 instruments 先行落库，bars 才能通过 FK 检查。
        await s.commit()

    async with AsyncSessionLocal() as s:
        # BarDaily 幂等：按 (instrument_id, trade_date) PK 查询，不存在才创建。
        # 每只标的生成 HISTORY_BARS 根 <= T 的历史日线（T, T-1, ..., T-59），
        # 满足 _classify_computable_universe 的 >= 60 阈值（正常 computable universe）。
        history_dates = [T - timedelta(days=n) for n in range(HISTORY_BARS)]
        for iid in inst_ids:
            for d in history_dates:
                exists = (
                    await s.execute(
                        select(BarDaily).where(
                            BarDaily.instrument_id == iid,
                            BarDaily.trade_date == d,
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    s.add(
                        BarDaily(
                            instrument_id=iid, trade_date=d,
                            close=Decimal("10.00"), adj_factor=Decimal("1.0"),
                        )
                    )

        # canonical DSA definition（create_batch_run 以 strategy_key 解析策略；
        # 验证库为空库，必须自备 dsa_selector 定义 + released version，且幂等）。
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
    """succeeded CoreRun + core run items + 真实可解码 artifact snapshots。

    不创建 MarketReviewRun（PG 本轮不证明 Review domain；由既有 T3 Case E 覆盖）。
    """
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
            # 正式 codec：coreArtifact + dsaProjection 两个独立块，lineage 完全一致。
            # iter_core_artifacts 只读取 dsaProjection 块；coreArtifact 块镜像完整
            # core artifact（decode_core_artifact_from_summary 需要）。
            param_hash = f"ph-{idx}"
            algo_versions = {"dsa": version.version}
            in_hash = f"in-{idx}"
            bars_hash = f"bars-{idx}"
            adj_hash = f"adj-{idx}"
            src = str(run.id)
            core_block = encode_core_artifact_to_summary(
                schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
                first_pyramid_core={"trend": {"availability": "ready"}, "nBars": 250},
                structural_payload={"dsa_segment": {"seg": idx + 1}},
                dsa_projection_payload={"dsa_vwap": 10.5, "dsa_dir_bars": 12},
                dsa_visual_contract={"dsa_vwap": 10.5},
                state_event_candidates=[{"type": "structure_break", "t": str(T)}],
                availability={
                    "trend": "ready", "structure": "ready", "momentum": "ready",
                },
                parameter_hash=param_hash,
                source_core_run_id=src,
                algorithm_versions=algo_versions,
                input_hash=in_hash,
                bars_hash=bars_hash,
                adj_factor_hash=adj_hash,
                diagnostics={"dsa": 1, "smc": 1},
            )
            dsa_block = encode_dsa_projection_to_summary(
                schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
                dsa_projection_payload={"dsa_vwap": 10.5, "dsa_dir_bars": 12},
                dsa_visual_contract={"dsa_vwap": 10.5},
                availability={
                    "trend": "ready", "structure": "ready", "momentum": "ready",
                },
                parameter_hash=param_hash,
                source_core_run_id=src,
                algorithm_versions=algo_versions,
                input_hash=in_hash,
                bars_hash=bars_hash,
                adj_factor_hash=adj_hash,
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
                    structural_payload={},
                    temporal_payload={},
                    summary_payload={
                        "coreArtifact": core_block,
                        "dsaProjection": dsa_block,
                    },
                )
            )
        # SchedulerJobRun（PG-B 投影需要 job_run_id；不属于 Review domain）。
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


# ---------------------------------------------------------------------------
# PG-A
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_a_compat_create_batch_run_lineage_and_universe():
    inst_ids, _ver = await _seed_base_world()
    # [§4] 所有操作在有效 async session 作用域内完成；禁止退出 context 后复用已关闭 session。
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
        # [§5] instrument_ids 必须是 list[uuid.UUID]（生产 create_batch_run 的 domain contract），
        # 禁止 list[str]。
        compat = await svc.create_batch_run(
            db=s,
            strategy_key=DSA_SELECTOR,
            trade_date=T,
            run_type="scheduled",
            instrument_ids=inst_ids,
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

    # [§12] 查询 StrategyRunItem：正常 computable universe 下应全 pending，0 skipped。
    async with AsyncSessionLocal() as s:
        items = (
            await s.execute(
                select(StrategyRunItem).where(StrategyRunItem.run_id == compat.id)
            )
        ).scalars().all()
    assert len(items) == len(inst_ids), "run_items 数必须 == universe"
    assert all(it.status == "pending" for it in items), (
        "正常 universe 的 run_items 应为 pending，而非 insufficient_history skipped"
    )
    assert not any(it.status == "skipped" for it in items), (
        "fixture 必须满足正常 computable universe（每只 >= 60 根历史日线）"
    )


# ---------------------------------------------------------------------------
# PG-B
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_b_full_projection_publish_readback():
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
        instrument_ids=inst_ids,  # [§5] list[uuid.UUID]，非 list[str]
    )
    assert result["status"] == "succeeded", result
    assert result.get("published_at")

    # 新 session 读回持久化事实
    async with AsyncSessionLocal() as pdb:
        row = await pdb.get(StrategyRun, uuid.UUID(result["dsa_run_id"]))
        assert row.status == "published"
        assert row.published_at is not None
        assert (row.input_overrides or {}).get("requirement") == "required_compatibility"
        # [§12] 计数合同
        assert row.succeeded_count == len(inst_ids)
        assert row.failed_count == 0
        assert row.skipped_count == 0
        sres_count = (
            await pdb.execute(
                select(func.count()).select_from(StrategyResult).where(
                    StrategyResult.run_id == row.id,
                )
            )
        ).scalar_one()
        assert sres_count == len(inst_ids), "StrategyResult 数必须 == universe"

        c2 = await pdb.get(StockFeatureSnapshotRun, core_x)
        assert c2.status == "succeeded", "Core 不被兼容性成功撤销"


# ---------------------------------------------------------------------------
# PG-C
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pg_c_projection_failure_preserves_lineage():
    inst_ids, _ver = await _seed_base_world()
    # succeeded CoreRun，合法 2-stock universe，但**无任何 snapshot 行**
    # → 投影零产物 → 未达 completed → raise（兼容性 optional failed）。
    async with AsyncSessionLocal() as s:
        run = StockFeatureSnapshotRun(
            trade_date=T, run_type="after_close", status="succeeded",
            started_at=datetime.now().astimezone(),
            finished_at=datetime.now().astimezone(),
        )
        s.add(run)
        await s.commit()
        core_x = run.id
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
        job_id = job.id

    # [§13] instrument_ids=inst_ids（真实 universe，非空）：失败归因于 artifact 缺失，
    # 而非 total_instruments=0。
    with pytest.raises(RuntimeError):
        await _run_dsa_compatibility_projection(
            job_run_id=job_id,
            worker_id="pg-c",
            lease_epoch=None,
            trade_date=T,
            snapshot_run_id=core_x,
            dsa_run_id=None,
            instrument_ids=inst_ids,
        )

    # Core 不被撤销；兼容性 run 不产生 published 成功事实。
    # [§11] 不检查 MarketReviewRun（PG 本轮不证明 Review domain）。
    async with AsyncSessionLocal() as cdb:
        c2 = await cdb.get(StockFeatureSnapshotRun, core_x)
        assert c2.status == "succeeded", "Core 不被兼容性失败撤销"

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
